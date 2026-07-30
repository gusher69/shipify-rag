"""Semantic API Analysis Engine — the ONE place that decides "what
intent/role/entity does this field or endpoint have" for the AI-Guided
ERP Setup (setup-time only; never a runtime/Decision-Engine dependency).

Root cause this module fixes: THREE previously-disconnected
classification systems used to coexist —
  1. ai_auto_setup_service.py's hardcoded ``_INTENT_TAXONOMY`` /
     ``_SYNONYM_TABLE`` regex list (per-domain, endpoint-specific —
     REMOVED, not extended, see that module's ``classify_intent()``).
  2. The LLM's own free-form ``action_type``/``detected_action_type``
     (non-deterministic, unconstrained vocabulary, no confidence).
  3. ``erp_test_harness.py::infer_operation_type_with_evidence()`` — the
     already-built, deterministic, evidence-ranked, well-tested
     classifier used at RUNTIME by the Decision Engine / conversation
     defaults. That function's vocabulary/behavior is NOT touched here
     (large blast radius) — this module adds a SEPARATE, setup-time-only
     sibling (`classify_endpoint_intent`) that reuses the exact same
     evidence-tier PRINCIPLE (structural signal > verb-pattern > weak
     text > safe default) but returns a richer vocabulary purpose-built
     for the AI-Guided Setup wizard.

Every function in this module is a PURE, deterministic transformation —
regex/structural pattern matching only, never a fresh LLM call — so the
same input always produces byte-identical output (Step 10). No field
name (CustCode/OrderNo/TrackingNo/etc.) is ever compared literally in
logic anywhere below; all detection is via generic structural/regex
patterns, reused from services/erp_test_harness.py wherever an
equivalent generic primitive already exists (semantic_classify,
detect_entities, _primary_entity) instead of re-implementing it.
"""
import re
from typing import Dict, List, Optional, Tuple

from services import erp_test_harness as harness
from services.ai_auto_setup_service import (
    _SECRET_NAME_RE, redact_secrets, infer_input_source, _infer_param_type, _humanize_name,
)
from services.business_action_registry import GROUP_RULES

ANALYSIS_VERSION = "1.0"
ANALYZER_VERSION = "semantic_api_analysis_engine@1.0"

# ── Step 1: Semantic Endpoint Classification ────────────────────────────

ENDPOINT_INTENTS = (
    "LOOKUP", "SEARCH", "LIST", "DETAIL", "TRANSFORM", "COMMAND", "NOTIFICATION",
    "MUTATION", "UPLOAD", "DOWNLOAD", "AUTHENTICATION", "HEALTHCHECK", "UTILITY", "UNKNOWN",
)

# Generic verb-pattern table (endpoint name/description text only) — a
# superset/remap of erp_test_harness._OPERATION_VERB_PATTERNS' vocabulary
# for the richer setup-time taxonomy this engine returns.
_ENDPOINT_VERB_PATTERNS = [
    ("SEARCH", re.compile(r"\bsearch\b|\bfind\b|\bquery\b|ค้นหา", re.IGNORECASE)),
    ("LIST", re.compile(r"\blist\b|\ball\b|รายการ", re.IGNORECASE)),
    ("TRANSFORM", re.compile(r"\bconvert\b|\btransform\b|\bparse\b|\bextract\b|แปลง", re.IGNORECASE)),
    ("NOTIFICATION", re.compile(r"\bnotify\b|\bnotification\b|\balert\b|\bsend\b.*\b(message|noti)\b|แจ้งเตือน", re.IGNORECASE)),
    ("UPLOAD", re.compile(r"\bupload\b|\battach\b|อัปโหลด", re.IGNORECASE)),
    ("DOWNLOAD", re.compile(r"\bdownload\b|\bexport\b|ดาวน์โหลด", re.IGNORECASE)),
    ("AUTHENTICATION", re.compile(r"\blogin\b|\bauth\b|\btoken\b|\bsign[_-]?in\b|เข้าสู่ระบบ", re.IGNORECASE)),
    ("HEALTHCHECK", re.compile(r"\bping\b|\bhealth\b|\bstatus[_-]?check\b|\buptime\b", re.IGNORECASE)),
    ("LOOKUP", re.compile(r"\bget\b|\bfetch\b|\bretrieve\b|\bdetail\b|ดึงข้อมูล|รายละเอียด", re.IGNORECASE)),
]

_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _split_identifier_words(s: str) -> str:
    """Generic PascalCase/camelCase tokenizer (e.g. 'GetUrlProductDetail'
    -> 'Get Url Product Detail'). Most real ERP endpoint names/URL path
    segments are concatenated identifiers with no spaces — without this,
    every verb-pattern regex below silently never matches ANY endpoint
    named this way, regardless of vendor. Purely structural (word-
    boundary insertion), never a per-word/per-endpoint lookup."""
    return _CAMEL_SPLIT.sub(" ", s or "")


_MUTATING_VERB_PATTERN = re.compile(
    r"\bcreate\b|\badd\b|\bupdate\b|\bedit\b|\bmodify\b|\bcancel\b|\bdelete\b|\bremove\b|\bvoid\b|"
    r"สร้าง|เพิ่ม|แก้ไข|ยกเลิก|ลบ", re.IGNORECASE,
)
_COLLECTION_HINT_KEYS = ("items", "data", "list", "results", "rows", "records")
_URL_SHAPED = re.compile(r"^https?://|\.(jpg|jpeg|png|gif|pdf|html?)(\?|$)", re.IGNORECASE)


def _response_shape(example_response) -> str:
    """Purely structural: 'collection' / 'single_object' / 'none'. Never
    inspects field NAMES — only Python container types and, for a
    dict-wrapper, whether one of its values is itself a list (a
    "paginated wrapper" shape covered generically, not by key name)."""
    if example_response is None:
        return "none"
    if isinstance(example_response, list):
        return "collection"
    if isinstance(example_response, dict):
        if any(isinstance(v, list) for v in example_response.values()):
            return "collection"
        return "single_object"
    return "none"


def _has_url_shaped_input(body_fields: List[Dict]) -> bool:
    for f in body_fields or []:
        example = f.get("example_value") if isinstance(f, dict) else None
        name = f.get("name", "") if isinstance(f, dict) else ""
        if (example and _URL_SHAPED.search(str(example))) or re.search(r"url|link$", (name or "").lower()):
            return True
    return False


def classify_endpoint_intent(
    endpoint_url: str = "", http_method: str = "GET", description: str = "",
    headers: Optional[Dict] = None, body_fields: Optional[List[Dict]] = None,
    example_request: Optional[Dict] = None, example_response=None,
    explicit_override: Optional[str] = None,
) -> Dict:
    """Step 1 — Semantic Endpoint Classification. Returns
    {"intent": ..., "confidence": float, "evidence": [...]}. Evidence-tier
    priority (generic, no per-endpoint hardcoding):
      1. caller-supplied explicit override (conf 1.0)
      2. HTTP method + response-shape signals combined
      3. verb-pattern match against endpoint name/description
      4. weak description-text evidence (never alone -> MUTATION/COMMAND)
      5. UNKNOWN, review recommended
    """
    if explicit_override:
        return {"intent": explicit_override, "confidence": 1.0, "evidence": ["explicit caller override"]}

    method = (http_method or "GET").upper()
    # Tokenize the endpoint URL/name (usually PascalCase-concatenated,
    # e.g. "SearchDataShipmentList") so verb-pattern regexes below can
    # match ANY vendor's naming convention generically.
    text = " ".join([_split_identifier_words(endpoint_url or ""), description or ""])
    shape = _response_shape(example_response)
    body_fields = body_fields or []
    url_to_structured = _has_url_shaped_input(body_fields) and shape in ("single_object", "collection")

    # Tier 2 — structural: URL-shaped input -> structured output is a
    # strong, generic TRANSFORM signal regardless of HTTP method/verb.
    if url_to_structured and method == "GET":
        return {"intent": "TRANSFORM", "confidence": 0.9,
                "evidence": ["URL/link-shaped input field maps to a structured response (GET)"]}

    # A destructive-shaped POST/PUT/PATCH verb signal outranks a
    # collection/single-object response shape (a CANCEL/DELETE call can
    # still echo back an updated list/object) — checked first.
    if method in ("POST", "PUT", "PATCH") and _MUTATING_VERB_PATTERN.search(text):
        return {"intent": "MUTATION", "confidence": 0.85,
                "evidence": [f"{method} + state-mutating verb in name/description"]}

    # Tier 2 — HTTP method + response-shape signals combined. Deliberately
    # applies to BOTH GET and POST/PUT/PATCH: many real ERP "Get*"/
    # "Search*" style read endpoints use POST for their body — method
    # alone is never dispositive, the SHAPE of what comes back is.
    if method in ("GET", "POST", "PUT", "PATCH"):
        if shape == "collection":
            for op, pattern in _ENDPOINT_VERB_PATTERNS:
                if op in ("SEARCH", "LIST") and pattern.search(text):
                    return {"intent": op, "confidence": 0.95,
                            "evidence": [f"{method} + collection-shaped example response + '{op}' verb match"]}
            return {"intent": "LIST", "confidence": 0.8,
                    "evidence": [f"{method} + collection-shaped example response"]}
        if shape == "single_object":
            for op, pattern in _ENDPOINT_VERB_PATTERNS:
                if op in ("LOOKUP",) and pattern.search(text):
                    return {"intent": "DETAIL" if re.search(r"\bdetail\b|รายละเอียด", text, re.IGNORECASE) else "LOOKUP",
                             "confidence": 0.9,
                             "evidence": [f"{method} + single-object example response + '{op}' verb match"]}
            return {"intent": "LOOKUP", "confidence": 0.75,
                    "evidence": [f"{method} + single-object example response"]}

    if method in ("POST", "PUT", "PATCH"):
        for op, pattern in _ENDPOINT_VERB_PATTERNS:
            if op == "NOTIFICATION" and pattern.search(text):
                return {"intent": "NOTIFICATION", "confidence": 0.9,
                        "evidence": [f"{method} + notification verb match, no persisted-looking response"]}
        for op, pattern in _ENDPOINT_VERB_PATTERNS:
            if op in ("UPLOAD", "AUTHENTICATION") and pattern.search(text):
                return {"intent": op, "confidence": 0.85,
                        "evidence": [f"{method} + '{op}' verb match, no persisted-looking response"]}

    # Tier 3a — a URL/link-shaped input field is a strong, generic
    # TRANSFORM signal on its own (converting a URL into structured data
    # is definitionally a transform, regardless of vendor/wording) even
    # when no example response was ever captured to confirm the output
    # shape (Tier 2 requires one) — outranks the generic "get"-style verb
    # match below, which would otherwise mislabel this LOOKUP.
    if _has_url_shaped_input(body_fields):
        return {"intent": "TRANSFORM", "confidence": 0.65,
                "evidence": ["URL/link-shaped input field present; no example response available to confirm "
                              "output shape - inferred TRANSFORM from the input signal alone"]}

    # Tier 3 — verb pattern against name/description, any HTTP method
    # (a read-oriented "Get*"/"Search*"-style capability that happens to
    # use POST/PUT for its body is still a LOOKUP/SEARCH/LIST/TRANSFORM —
    # method alone is never dispositive without a matching verb).
    for op, pattern in _ENDPOINT_VERB_PATTERNS:
        if pattern.search(text):
            confidence = 0.6 if op in ("MUTATION",) else 0.7
            return {"intent": op, "confidence": confidence, "evidence": [f"verb-pattern match for '{op}'"]}

    if method in ("POST", "PUT", "PATCH"):
        return {"intent": "COMMAND", "confidence": 0.5,
                "evidence": [f"{method} with no persisted-looking response and no verb-pattern match — weak COMMAND signal"]}

    # Tier 4 — weak description-only evidence, never alone -> destructive/mutating.
    if description and _MUTATING_VERB_PATTERN.search(description):
        return {"intent": "UTILITY", "confidence": 0.4,
                "evidence": ["weak description-text evidence only — mutation wording present but not "
                              "structurally confirmed; downgraded to UTILITY rather than MUTATION"]}

    if method == "GET":
        return {"intent": "LOOKUP", "confidence": 0.5, "evidence": ["GET method, no stronger signal available"]}

    return {"intent": "UNKNOWN", "confidence": 0.3,
            "evidence": ["no confident structural or verb-pattern signal found — review recommended"]}


# ── Step 2: Field Semantic Classification (role taxonomy) ──────────────

FIELD_ROLES = (
    "authentication", "identifier", "customer_identifier", "order_identifier",
    "tracking_identifier", "product_identifier", "search", "filter", "date_start",
    "date_end", "enum", "limit", "pagination", "payload", "attachment", "url",
    "phone", "email", "message", "system_hidden", "credential", "response_only", "derived",
)

_DATE_START_SUFFIX = re.compile(r"(start|from|since)$", re.IGNORECASE)
_DATE_END_SUFFIX = re.compile(r"(end|to|until)$", re.IGNORECASE)
_LIMIT_NAME = re.compile(r"limit$|^top$|^latest$|^max|^page[_-]?size$|take$", re.IGNORECASE)
_PAGINATION_NAME = re.compile(r"page$|^offset$|cursor$|page[_-]?(no|number|index)$", re.IGNORECASE)
_ATTACHMENT_NAME = re.compile(r"attach|file$|filename|image$|photo$|document$", re.IGNORECASE)
_ENUM_HINT = re.compile(r"status$|type$|category$|^is[_A-Z]", re.IGNORECASE)
_DATE_SHAPED_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}|^\d{2}/\d{2}/\d{4}")


def classify_field_roles(field_name: str, field_description: str = "", example_value=None,
                          context: Optional[Dict] = None) -> List[str]:
    """Step 2 — a field MAY carry multiple roles. `context` is any of the
    already-computed, generic signals a caller has available: {"entity":
    str|None, "http_method": str, "response_only": bool, "action": dict}
    — never a literal per-field-name lookup table."""
    context = context or {}
    roles: List[str] = []
    name = field_name or ""
    lower = name.lower()
    desc = (field_description or "")

    # Authentication/credential/system_hidden — SAME detection erp_test_
    # harness's siblings (redact_secrets/infer_input_source) already use,
    # reused rather than re-implemented.
    if _SECRET_NAME_RE.search(name) or infer_input_source(name) == "secret_configuration":
        roles += ["authentication", "credential", "system_hidden"]

    sem = harness.semantic_classify(name, fallback_entity=context.get("entity"))
    semantic_type = sem["semantic_type"]
    entity = context.get("entity") or sem["entity"]

    if semantic_type == "identifier":
        roles.append("identifier")
        if entity == "customer":
            roles.append("customer_identifier")
        elif entity == "order":
            roles.append("order_identifier")
        elif entity == "tracking":
            roles.append("tracking_identifier")
        elif entity == "product":
            roles.append("product_identifier")

    if semantic_type == "email":
        roles.append("email")
    if semantic_type == "phone":
        roles.append("phone")
    if semantic_type == "url":
        roles.append("url")

    # Date-range roles — SUFFIX pattern + date-shaped example value,
    # never a literal field-name comparison.
    date_shaped = bool(example_value and _DATE_SHAPED_VALUE.search(str(example_value))) or semantic_type == "date"
    if date_shaped and _DATE_START_SUFFIX.search(lower):
        roles.append("date_start")
    elif date_shaped and _DATE_END_SUFFIX.search(lower):
        roles.append("date_end")

    if _LIMIT_NAME.search(lower):
        roles.append("limit")
    if _PAGINATION_NAME.search(lower):
        roles.append("pagination")
    if _ATTACHMENT_NAME.search(lower):
        roles.append("attachment")
    if _ENUM_HINT.search(lower) and semantic_type == "string":
        roles.append("enum")
    if re.search(r"message$|body$|text$|content$", lower) or re.search(r"message|ข้อความ", desc, re.IGNORECASE):
        roles.append("message")
    if re.search(r"^q$|query$|keyword$|search", lower):
        roles.append("search")
    if re.search(r"filter", lower) or (roles and "date_start" in roles or "date_end" in roles):
        if "filter" not in roles and re.search(r"filter|status$|category$", lower):
            roles.append("filter")

    if context.get("response_only"):
        roles.append("response_only")
    if context.get("derived"):
        roles.append("derived")

    if semantic_type in ("string", "value") and not roles:
        roles.append("payload")
    elif not roles:
        roles.append("payload")

    # dedupe, keep first-seen order
    seen, out = set(), []
    for r in roles:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# ── Step 3: Validation Groups ────────────────────────────────────────────

_AT_LEAST_ONE_PHRASE = re.compile(
    r"at\s*least\s*one|one\s*of|either\s+.+\s+or|เลือกอย่างน้อย\s*1|อย่างน้อยหนึ่ง", re.IGNORECASE,
)


def infer_validation_groups(fields: List[Dict]) -> List[Dict]:
    """Step 3 — infers ALL/AT_LEAST_ONE/EXACTLY_ONE/OPTIONAL groups
    (reusing GROUP_RULES exactly) from generic phrase-detection in field
    descriptions, or from a set of required fields sharing common
    substitutable-search-criteria wording. Returns a list of
    {"rule": ..., "members": [...], "confidence": float, "source": ...}.
    Never fabricates a rule when only weakly inferable — that case is
    surfaced as a low-confidence suggestion instead (Step 11 review)."""
    groups: List[Dict] = []
    phrase_hit_members = [f["name"] for f in fields
                           if isinstance(f, dict) and _AT_LEAST_ONE_PHRASE.search(f.get("description") or "")]
    if len(phrase_hit_members) >= 2:
        groups.append({"rule": "AT_LEAST_ONE", "members": phrase_hit_members, "confidence": 0.9,
                        "source": "shared 'at least one of' phrase in field descriptions"})

    # Weak structural fallback: >=2 required fields with the SAME
    # semantic_type/entity role that aren't already grouped, flagged as
    # a low-confidence suggestion for human review rather than asserted.
    # Authentication/credential fields are NEVER eligible — a secret is
    # not a substitutable search criterion alongside a real business
    # identifier, regardless of what semantic_classify's generic
    # identifier-name pattern happens to also match on its field name.
    remaining = [f for f in fields if isinstance(f, dict) and f.get("name") not in phrase_hit_members
                 and f.get("required")
                 and not ({"credential", "authentication", "system_hidden"} & set(f.get("roles") or []))]
    by_role: Dict[str, List[str]] = {}
    for f in remaining:
        roles = f.get("roles") or []
        for r in roles:
            if r.endswith("_identifier"):
                by_role.setdefault(r, []).append(f["name"])
    for role, members in by_role.items():
        if len(members) >= 2:
            groups.append({"rule": "AT_LEAST_ONE", "members": members, "confidence": 0.4,
                            "source": f"low-confidence suggestion: multiple required '{role}' fields "
                                      "look substitutable — needs human review"})
    return groups


# ── Step 4: Authentication Detection (verification only — no rebuild) ──

def field_is_authentication(field_name: str) -> bool:
    """Thin, explicit wrapper proving Step 4's requirement: authentication/
    credential fields are detected the SAME way redact_secrets()/
    infer_input_source() already do — never a second, competing pattern."""
    return bool(_SECRET_NAME_RE.search(field_name or "")) or infer_input_source(field_name) == "secret_configuration"


# ── Step 5: Business Entity Detection (presentation layer only) ────────

def describe_business_entity(field_name: str, roles: List[str], entity: Optional[str],
                               endpoint_intent: Optional[str] = None) -> str:
    """Step 5 — a thin, generic presentation layer over already-computed
    roles/entity/semantic_type. Never a new hardcoded per-field-name
    lookup table: it composes a label purely from the generic
    entity string (from erp_test_harness.detect_entities/_primary_entity)
    and the generic role vocabulary (Step 2)."""
    sem = harness.semantic_classify(field_name, fallback_entity=entity)
    entity_label = (entity or sem["entity"] or "").replace("_", " ").strip()
    if "message" in roles and endpoint_intent == "NOTIFICATION":
        return "Notification Message"
    if "url" in roles:
        return f"{entity_label.title()} URL".strip() if entity_label and entity_label != "general" else "URL"
    if entity_label and entity_label != "general":
        attr = sem["canonical_name"].split(".", 1)[-1]
        if any(r.endswith("_identifier") for r in roles) or attr == "identifier":
            return entity_label.title()
        return f"{entity_label.title()} {attr.replace('_', ' ').title()}".strip()
    return _humanize_name(field_name)


# ── Step 6: Response Semantic Analysis ──────────────────────────────────

_IMAGE_EXT = re.compile(r"\.(jpg|jpeg|png|gif|webp|bmp)(\?|$)", re.IGNORECASE)


def analyze_response_semantics(example_response=None, fallback_entity: Optional[str] = None) -> Dict:
    """Step 6 — classifies the top-level shape and, for a dict-shaped
    example, each field's semantic type (extended with image/attachment/
    nested-object detection). Returns
    {"response_mapping_status": "analyzed"|"pending", ...}. NEVER fails,
    NEVER fabricates fields when no example is given."""
    if example_response is None:
        return {"response_mapping_status": "pending", "reason": "no example response provided"}

    shape = _response_shape(example_response)
    if isinstance(example_response, list):
        sample = example_response[0] if example_response and isinstance(example_response[0], dict) else {}
    elif isinstance(example_response, dict):
        # A dict wrapper whose OWN top-level fields already are the
        # sample (e.g. {"Items": [...], "CustName": "x"}) — analyze its
        # own keys directly rather than descending into the nested list,
        # so a field like "Items" is itself reported as a collection.
        sample = example_response
    else:
        sample = {}

    fields = []
    for key, value in (sample.items() if isinstance(sample, dict) else []):
        sem = harness.semantic_classify(key, fallback_entity=fallback_entity)
        semantic_type = sem["semantic_type"]
        if isinstance(value, str) and _IMAGE_EXT.search(value):
            semantic_type = "image"
        elif isinstance(value, str) and _URL_SHAPED.search(value):
            semantic_type = "attachment" if _ATTACHMENT_NAME.search(key.lower()) else sem["semantic_type"]
        elif isinstance(value, dict):
            semantic_type = "nested_object"
        elif isinstance(value, list):
            semantic_type = "collection"
        fields.append({"field": key, "semantic_type": semantic_type, "canonical_name": sem["canonical_name"]})

    return {
        "response_mapping_status": "analyzed",
        "shape": shape,
        "fields": fields,
    }


# ── Step 7: Operation Safety ─────────────────────────────────────────────

_DESTRUCTIVE_HINT = re.compile(r"\bcancel\b|\bdelete\b|\bremove\b|\bvoid\b|ยกเลิก|ลบ", re.IGNORECASE)


def derive_operation_safety(endpoint_intent: str, endpoint_text: str = "") -> Dict:
    """Step 7 — derived generically from Step 1's intent (reusing the
    same destructive-verb CONCEPT as erp_test_harness's
    `_DESTRUCTIVE_OPERATION_TYPES`, applied to the richer vocabulary)."""
    read_only = endpoint_intent in ("LOOKUP", "SEARCH", "LIST", "DETAIL", "HEALTHCHECK", "TRANSFORM")
    state_change = endpoint_intent in ("MUTATION", "COMMAND", "UPLOAD")
    notification = endpoint_intent == "NOTIFICATION"
    destructive = state_change and bool(_DESTRUCTIVE_HINT.search(endpoint_text or ""))
    financial = bool(re.search(r"pay|refund|wallet|balance|charge|เงิน|ชำระ", endpoint_text or "", re.IGNORECASE))
    return {
        "read_only": read_only,
        "state_change": state_change,
        "financial": financial,
        "notification": notification,
        "destructive": destructive,
        "confirmation_required": notification or state_change,
        "rollback_recommended": destructive,
        "audit_required": destructive or notification or financial,
    }


# ── Step 8: AI Suggestions (actionable, generically triggered) ─────────

_SEARCH_LIKE_INTENTS = ("SEARCH", "LIST")

# Any of these roles constitutes a genuine search/filter criterion for a
# SEARCH/LIST endpoint — not just a field literally role-tagged "search"/
# "filter". A date range (date_start/date_end), an enum-valued status
# filter, a result limiter (limit), or a pagination cursor are all
# criteria a customer/admin narrows or pages a search by, generically,
# regardless of field name.
_SEARCH_CRITERIA_ROLES = ("search", "filter", "date_start", "date_end", "enum", "limit", "pagination")

# Severity levels (Review & Edit UI hardening sprint, Step 6) — every
# recommendation carries one, computed here (single source of truth,
# never re-classified by string-matching in the frontend).
RECOMMENDATION_SEVERITIES = ("info", "recommendation", "warning", "error")


def build_recommendations_detailed(*, response_analysis: Dict, endpoint_intent_result: Dict,
                                    fields: List[Dict], operation_safety: Dict,
                                    avoid_phrase_overlap: bool = False) -> List[Dict]:
    """Step 8 (Review & Edit UI hardening: severity-tagged variant) —
    every trigger below is gated on the endpoint's OWN detected intent/
    roles, never a blanket check applied regardless of what kind of
    endpoint this is. In particular: the "no search/filter fields
    detected" warning is scoped to SEARCH/LIST intents only — a COMMAND/
    NOTIFICATION endpoint (e.g. SendLineNotiCS, whose only required
    field is a message payload, not a search criterion) must never
    surface it — and it recognizes ANY of `_SEARCH_CRITERIA_ROLES`
    (search/filter/date_start/date_end/enum/limit/pagination) as a
    genuine search criterion, not just fields literally tagged "search"/
    "filter". Each entry is {"message": str, "severity": one of
    RECOMMENDATION_SEVERITIES} — severity is decided HERE, once, so the
    UI never re-classifies a message by string-matching."""
    detailed: List[Dict] = []

    if response_analysis.get("response_mapping_status") == "pending":
        detailed.append({"message": "Response mapping incomplete - provide a sample response to complete field mapping",
                          "severity": "info"})
    if endpoint_intent_result.get("confidence", 1.0) < 0.6:
        detailed.append({"message": "Endpoint intent confidence low - review classification", "severity": "info"})
    if endpoint_intent_result.get("intent") in _SEARCH_LIKE_INTENTS:
        has_search_criteria = any(
            any(role in _SEARCH_CRITERIA_ROLES for role in (f.get("roles") or [])) for f in fields)
        if not has_search_criteria:
            detailed.append({"message": "No search/filter fields detected for a SEARCH/LIST endpoint - review field roles",
                              "severity": "warning"})
    if operation_safety.get("notification") or operation_safety.get("destructive"):
        if not operation_safety.get("confirmation_required"):
            # A notification/destructive operation with confirmation NOT
            # required is a genuine safety gap, not a mere suggestion.
            detailed.append({"message": "Confirmation is not required for a notification/destructive operation - "
                                          "this is a safety gap, review before enabling", "severity": "error"})
        else:
            detailed.append({"message": "Confirmation recommended before execution", "severity": "recommendation"})
    if any("credential" in (f.get("roles") or []) for f in fields):
        detailed.append({"message": "Credential should be hidden - verify input_source is credential_store",
                          "severity": "warning"})
    if avoid_phrase_overlap:
        detailed.append({"message": "Hybrid KB recommended - existing Knowledge Base content overlaps with this capability",
                          "severity": "recommendation"})
    return detailed


def build_recommendations(*, response_analysis: Dict, endpoint_intent_result: Dict,
                           fields: List[Dict], operation_safety: Dict,
                           avoid_phrase_overlap: bool = False) -> List[str]:
    """Backward-compatible plain-string form — existing callers/tests
    (predating the severity-tagged Review & Edit UI) keep working
    unchanged. Thin wrapper over `build_recommendations_detailed()`
    (single source of truth, never a second, drifting implementation)."""
    return [d["message"] for d in build_recommendations_detailed(
        response_analysis=response_analysis, endpoint_intent_result=endpoint_intent_result,
        fields=fields, operation_safety=operation_safety, avoid_phrase_overlap=avoid_phrase_overlap)]


# ── Runtime bridge: maps this engine's richer Step-1 vocabulary onto the
# ALREADY-BUILT, well-tested runtime vocabulary
# (erp_test_harness.OPERATION_TYPES) so the setup-time classification
# flows into the Generic Integration Runtime through the EXISTING
# `action.setup_metadata.operation_type` override tier (see
# erp_test_harness.infer_operation_type_with_evidence, tier 2) — no
# change to that function's code/vocabulary/tests, no second competing
# runtime classifier, no blast radius on the dozens of existing
# runtime/Decision-Engine tests that depend on its current behavior.
_INTENT_TO_RUNTIME_OPERATION_TYPE = {
    "LOOKUP": "LOOKUP", "DETAIL": "LOOKUP", "SEARCH": "SEARCH", "LIST": "LIST",
    # TRANSFORM is now a first-class runtime operation (erp_test_harness.
    # OPERATION_TYPES) — identity mapping. Converting/parsing/extracting/
    # resolving/deriving information from an existing payload (URL
    # parsing, OCR normalization, data/currency conversion, date
    # formatting, JSON transformation) is NOT the same operation as
    # CALCULATION (computing a value); they must never collapse onto
    # the same runtime type. Do not remap this back to CALCULATION.
    "TRANSFORM": "TRANSFORM", "COMMAND": "WORKFLOW", "MUTATION": "UPDATE",
    "NOTIFICATION": "NOTIFICATION", "UPLOAD": "CREATE", "DOWNLOAD": "LOOKUP",
    "AUTHENTICATION": "WORKFLOW", "HEALTHCHECK": "LOOKUP", "UTILITY": "LOOKUP",
    "UNKNOWN": "UNKNOWN",
}


_CANCEL_HINT = re.compile(r"\bcancel\b|ยกเลิก", re.IGNORECASE)
_DELETE_HINT = re.compile(r"\bdelete\b|\bremove\b|\bvoid\b|ลบ", re.IGNORECASE)


def map_to_runtime_operation_type(intent: str, endpoint_text: str = "") -> str:
    """Maps a Step-1 `endpoint_intent` value onto the runtime's own
    `erp_test_harness.OPERATION_TYPES` vocabulary. A MUTATION whose
    endpoint text carries cancel/delete wording is mapped to the
    runtime's more specific CANCEL/DELETE type instead of the generic
    UPDATE, matching erp_test_harness._DESTRUCTIVE_OPERATION_TYPES'
    own, already-established distinction."""
    if intent == "MUTATION" and endpoint_text:
        if _CANCEL_HINT.search(endpoint_text):
            return "CANCEL"
        if _DELETE_HINT.search(endpoint_text):
            return "DELETE"
    return _INTENT_TO_RUNTIME_OPERATION_TYPE.get(intent, "UNKNOWN")


# ── Step 9: Exportable Metadata ──────────────────────────────────────────

def analyze_endpoint(
    endpoint_url: str = "", http_method: str = "GET", description: str = "",
    headers: Optional[Dict] = None, body_fields: Optional[List[Dict]] = None,
    example_request: Optional[Dict] = None, example_response=None,
    action: Optional[Dict] = None, avoid_phrase_overlap: bool = False,
) -> Dict:
    """Step 9 — the ONE canonical exportable JSON shape per analyzed
    endpoint, combining Steps 1-8. Designed to be embedded as an
    additive `semantic_analysis` key alongside the existing AI Auto
    Setup proposal shape (REQUIRED_TOP_LEVEL_KEYS/REQUIRED_PARAMETER_
    KEYS in ai_auto_setup_service.validate_proposal_schema stay
    untouched) — this is NOT a second competing contract format; see
    services/integration_contract_service.py for the runtime contract
    this setup-time artifact is meant to feed."""
    body_fields = body_fields or []
    action = action or {}
    entity = harness._primary_entity(action) if action else None
    if not entity:
        # fall back to entity detection purely from the field/description
        # text supplied directly to this call (no DB action required).
        pseudo_action = {"parameters": body_fields, "description": description, "display_name": endpoint_url}
        entity = harness._primary_entity(pseudo_action)

    endpoint_intent_result = classify_endpoint_intent(
        endpoint_url=endpoint_url, http_method=http_method, description=description,
        headers=headers, body_fields=body_fields, example_request=example_request,
        example_response=example_response,
    )

    field_results = []
    for f in body_fields:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        roles = classify_field_roles(f["name"], f.get("description", ""), f.get("example_value"),
                                      context={"entity": entity, "http_method": http_method})
        sem = harness.semantic_classify(f["name"], fallback_entity=entity)
        field_results.append({
            "name": f["name"],
            "roles": roles,
            "semantic_type": sem["semantic_type"],
            "canonical_name": sem["canonical_name"],
            "business_entity": describe_business_entity(f["name"], roles, entity, endpoint_intent_result["intent"]),
            "required": bool(f.get("required")),
            "description": f.get("description", ""),
        })

    validation_groups = infer_validation_groups(field_results)
    response_analysis = analyze_response_semantics(example_response, fallback_entity=entity)
    operation_safety = derive_operation_safety(endpoint_intent_result["intent"],
                                                endpoint_text=" ".join([endpoint_url or "", description or ""]))
    recommendations_detailed = build_recommendations_detailed(
        response_analysis=response_analysis, endpoint_intent_result=endpoint_intent_result,
        fields=field_results, operation_safety=operation_safety, avoid_phrase_overlap=avoid_phrase_overlap,
    )
    recommendations = [d["message"] for d in recommendations_detailed]

    # Runtime bridge value embedded directly in the canonical export (Step
    # 9) — additive key, so the UI/any consumer reads ONE object instead
    # of re-deriving this separately (no duplicated mapping logic).
    mapped_runtime_operation_type = map_to_runtime_operation_type(
        endpoint_intent_result["intent"], " ".join([endpoint_url or "", description or ""]))

    return {
        "endpoint_intent": endpoint_intent_result,
        "fields": field_results,
        "validation_groups": validation_groups,
        "response_analysis": response_analysis,
        "operation_safety": operation_safety,
        "recommendations": recommendations,
        "recommendations_detailed": recommendations_detailed,
        "entity": entity,
        "mapped_runtime_operation_type": mapped_runtime_operation_type,
        "analysis_version": ANALYSIS_VERSION,
        "analyzer_version": ANALYZER_VERSION,
    }


# ── Step 11: Interactive Review / Override Storage ──────────────────────
#
# Precedence model — reuses the EXACT 3-layer precedent from
# services/integration_schema_service.py::resolve_effective_integration_
# schema (explicit override -> derived default -> runtime default),
# applied here to per-field semantic analysis instead of conversation
# behavior:
#   1. RUNTIME DEFAULT   — this module's pure classification functions
#                           (classify_endpoint_intent/classify_field_roles/...)
#   2. DERIVED DEFAULT    — same as (1) here; the engine has no separate
#                           "action-structural" layer of its own the way
#                           operation_type does, so derived == runtime for
#                           this artifact (documented, not silently
#                           conflated: see `provenance` below).
#   3. EXPLICIT OVERRIDE  — an admin-confirmed value from
#                           `field_overrides`/`analysis_overrides`,
#                           reviewed on the Setup wizard's confirmation
#                           screen.
#
# Storage location (never overwrites a user-confirmed value on
# re-analysis):
#   - BEFORE save: the in-progress draft proposal dict carries an
#     additive `analysis_overrides` key, keyed by field name ->
#     {"role_override": [...], "intent_override": ..., ...}.
#   - AFTER save: persisted at `setup_metadata.semantic_overrides`
#     (mirrors the existing setup_metadata.routing/detection_reason
#     convention — no new DB column/table).
# `apply_overrides()` below performs the actual 3-layer merge and always
# reports provenance per field so the UI can show "auto-detected" vs.
# "admin-confirmed".

def apply_overrides(analysis: Dict, overrides: Optional[Dict] = None) -> Dict:
    """Merges admin-confirmed `overrides` over the auto-derived `analysis`
    (from analyze_endpoint()), NEVER mutating the input, and reports
    per-field/per-section provenance ("explicit" vs "derived") so the
    Review & Edit UI can show Detected / Override / Effective per field.

    Supported override keys (all optional, additive to the original
    Step 11 field_overrides/intent_override design — Review & Edit UI
    hardening sprint):
      - intent_override: str                 -> endpoint_intent.intent
      - operation_type_override: str          -> mapped_runtime_operation_type
      - confirmation_required_override: bool  -> operation_safety.confirmation_required
      - audit_required_override: bool         -> operation_safety.audit_required
      - validation_groups_override: List[Dict]-> validation_groups (replaces wholesale;
                                                  the UI always resends the full edited list)
      - field_overrides: {name: {"roles": [...], "business_entity": ...}}
    """
    overrides = overrides or {}
    merged = {**analysis, "fields": [dict(f) for f in analysis.get("fields", [])],
              "operation_safety": dict(analysis.get("operation_safety") or {}),
              "provenance": {}}

    if overrides.get("intent_override"):
        merged["endpoint_intent"] = {**analysis["endpoint_intent"], "intent": overrides["intent_override"],
                                      "confidence": 1.0, "evidence": ["admin-confirmed override"]}
        merged["provenance"]["endpoint_intent"] = "explicit"
    else:
        merged["provenance"]["endpoint_intent"] = "derived"

    if overrides.get("operation_type_override"):
        merged["mapped_runtime_operation_type"] = overrides["operation_type_override"]
        merged["provenance"]["mapped_runtime_operation_type"] = "explicit"
    else:
        merged["provenance"]["mapped_runtime_operation_type"] = "derived"

    if overrides.get("confirmation_required_override") is not None:
        merged["operation_safety"]["confirmation_required"] = bool(overrides["confirmation_required_override"])
        merged["provenance"]["operation_safety.confirmation_required"] = "explicit"
    else:
        merged["provenance"]["operation_safety.confirmation_required"] = "derived"

    if overrides.get("audit_required_override") is not None:
        merged["operation_safety"]["audit_required"] = bool(overrides["audit_required_override"])
        merged["provenance"]["operation_safety.audit_required"] = "explicit"
    else:
        merged["provenance"]["operation_safety.audit_required"] = "derived"

    if overrides.get("validation_groups_override") is not None:
        merged["validation_groups"] = overrides["validation_groups_override"]
        merged["provenance"]["validation_groups"] = "explicit"
    else:
        merged["provenance"]["validation_groups"] = "derived"

    field_overrides = overrides.get("field_overrides") or {}
    for f in merged["fields"]:
        name = f["name"]
        fo = field_overrides.get(name)
        if fo:
            if fo.get("roles") is not None:
                f["roles"] = fo["roles"]
            if fo.get("business_entity") is not None:
                f["business_entity"] = fo["business_entity"]
            merged["provenance"][f"fields.{name}"] = "explicit"
        else:
            merged["provenance"][f"fields.{name}"] = "derived"

    # Recommendations are recomputed from the EFFECTIVE (post-override)
    # state, not the original detected one — e.g. if the admin has
    # already set confirmation_required_override=True, the "safety gap"
    # error above must not still show once it's been addressed.
    merged["recommendations_detailed"] = build_recommendations_detailed(
        response_analysis=merged.get("response_analysis") or {}, endpoint_intent_result=merged["endpoint_intent"],
        fields=merged["fields"], operation_safety=merged["operation_safety"])
    merged["recommendations"] = [d["message"] for d in merged["recommendations_detailed"]]
    return merged


# ── Step 12: Example Generation (thin generic generators) ──────────────

_NEGATIVE_TEMPLATES_BY_ROLE = {
    "date_start": "malformed date value (e.g. '2026-13-45')",
    "date_end": "malformed date value (e.g. '2026-13-45')",
    "enum": "invalid enum value not in the allowed set",
    "email": "malformed email address (missing '@')",
    "phone": "malformed phone number (letters instead of digits)",
}


def generate_negative_test_cases(fields: List[Dict]) -> List[Dict]:
    """Step 12 — generic negative test cases derived purely from each
    field's role/semantic_type, never a hardcoded per-field-name list."""
    cases = []
    for f in fields:
        if f.get("required"):
            cases.append({"field": f["name"], "case": "required field missing",
                           "description": f"Omit '{f['name']}' and expect a validation error"})
        for role in f.get("roles", []):
            if role in _NEGATIVE_TEMPLATES_BY_ROLE:
                cases.append({"field": f["name"], "case": _NEGATIVE_TEMPLATES_BY_ROLE[role],
                              "description": f"Send {_NEGATIVE_TEMPLATES_BY_ROLE[role]} for '{f['name']}'"})
    return cases


def generate_boundary_test_cases(fields: List[Dict]) -> List[Dict]:
    """Step 12 — generic boundary cases from semantic_type/role."""
    cases = []
    for f in fields:
        roles = f.get("roles", [])
        if "limit" in roles or "pagination" in roles:
            cases.append({"field": f["name"], "case": f"{f['name']}=0", "description": "zero/lower-bound value"})
            cases.append({"field": f["name"], "case": f"{f['name']}=999999", "description": "very large value"})
        if "date_start" in roles or "date_end" in roles:
            cases.append({"field": f["name"], "case": "reversed date range",
                          "description": "date_start after date_end (reversed range)"})
    return cases


def generate_example_api_call(endpoint_url: str, http_method: str, fields: List[Dict]) -> Dict:
    """Step 12 — templated curl + JSON-body example built purely from
    already-analyzed fields + their example values, no new LLM call."""
    body = {f["name"]: f.get("example_value", f.get("name")) for f in fields
            if "credential" not in f.get("roles", []) and "authentication" not in f.get("roles", [])}
    method = (http_method or "GET").upper()
    curl = f"curl -X {method} '{endpoint_url}'"
    if body:
        curl += f" -H 'Content-Type: application/json' -d '{body}'"
    return {"curl": curl, "json_body": body, "method": method, "endpoint": endpoint_url}
