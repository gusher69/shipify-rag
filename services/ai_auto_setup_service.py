"""AI-Assisted API Auto Setup — turns a pasted endpoint/cURL/API doc/
OpenAPI/Postman snippet + a one-line business purpose into a structured
Business Action proposal an admin reviews and approves before anything is
saved to the Business Action Registry.

AI is used ONLY during configuration (this module). At runtime, the
Decision Engine / Information Collection / Executor all read the
Registry exclusively — this module never becomes a runtime dependency.

Calls the LLM exclusively through services/llm_service.py::get_llm_service()
— never a direct provider SDK call — per this codebase's own rule that
llm_service.py is the only place allowed to do that.
"""
import json
import re
from typing import Dict, List, Optional, Tuple

from services.business_action_registry import ACTION_TYPES, GROUP_RULES, HTTP_METHODS, INPUT_SOURCES
from services.llm_service import get_llm_service

# ── Secret redaction — applied BEFORE anything is sent to the LLM ────────
# Never sends a real secret value to the model. Deliberately broad
# (header-name based, not tied to one API's naming) so any pasted cURL/
# doc/Postman export gets the same treatment regardless of vendor.
_SECRET_HEADER_RE = re.compile(
    r"(Authorization|X-Api-Key|Api-Key|X-Auth-Token|X-Secret|Secret-Code|SecretCode)\s*:\s*\S.*",
    re.IGNORECASE,
)
_SECRET_PARAM_RE = re.compile(
    r"(secretcode|secret_code|api[_-]?key|apikey|token|access_token|client_secret|password|authorization)"
    r"\s*[:=]\s*[\"']?[\w\-\.]{4,}[\"']?",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"Bearer\s+[\w\-\.]+", re.IGNORECASE)


def redact_secrets(raw_text: str) -> Tuple[str, List[str]]:
    """Replaces anything that looks like a secret value with a
    placeholder, preserving header/parameter NAMES so the AI can still
    infer input_source/secret_ref correctly — only the value is redacted,
    never sent to the model. Returns (redacted_text, list of what kind of
    thing was redacted, for an audit trail / Developer Mode)."""
    if not raw_text:
        return raw_text, []
    redacted = raw_text
    found = []

    def _header_sub(m):
        found.append(m.group(1))
        return f"{m.group(1)}: [REDACTED]"

    redacted = _SECRET_HEADER_RE.sub(_header_sub, redacted)

    def _param_sub(m):
        found.append(m.group(1))
        sep = ":" if ":" in m.group(0) else "="
        return f"{m.group(1)}{sep}[REDACTED]"

    redacted = _SECRET_PARAM_RE.sub(_param_sub, redacted)
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    return redacted, found


def detect_and_redact_secrets(raw_text: str) -> Tuple[str, List[Dict]]:
    """The actual pre-LLM gate: runs the LOCAL, deterministic detector
    (services/secret_detector.py — cURL headers/form fields/JSON/query
    string/basic auth/doc-style "Name: value") FIRST, never relying on
    the LLM to find secrets. Each detected secret gets a masked preview
    and a suggested credential name for the Review screen. The legacy
    regex-based `redact_secrets()` then runs as a second pass over
    whatever text remains, purely as defense-in-depth for shapes the
    structured detector doesn't recognize — it never needs to report
    anything if the structured pass already caught everything.

    Returns (redacted_text, detected_credentials) where each entry is:
      {"name", "location", "masked_preview", "suggested_credential_key",
       "suggested_display_name"}
    — note: the RAW VALUE itself is deliberately not included in this
    return value; only the caller's local `structured` list temporarily
    holds it (never logged, never serialized) to perform the redaction.
    """
    from services.secret_detector import detect_secrets, redact_detected_secrets
    from services.credential_store import suggest_credential_key, suggest_display_name, mask_value

    structured = detect_secrets(raw_text)
    redacted, _log = redact_detected_secrets(raw_text, structured)
    # Second pass — catches anything shaped differently than the
    # structured detector's patterns (defense in depth only).
    redacted, _legacy_found = redact_secrets(redacted)

    detected_credentials = [
        {
            "name": s.name, "location": s.location, "masked_preview": mask_value(s.value),
            "suggested_credential_key": suggest_credential_key(s.name),
            "suggested_display_name": suggest_display_name(s.name),
        }
        for s in structured
    ]
    return redacted, detected_credentials


# ── Parameter source inference table (fallback only — the AI proposes
# input_source itself; this is a deterministic safety net used only to
# validate/backfill an AI response that's missing this field) ──────────
_SECRET_NAME_RE = re.compile(r"secret\s*code|secretcode|authorization|api[_-]?key|apikey|token|client[_-]?secret|password", re.IGNORECASE)
_PROFILE_NAME_RE = re.compile(r"^(line[_-]?user[_-]?id|customer[_-]?profile[_-]?id|internal[_-]?customer[_-]?id)$", re.IGNORECASE)


def infer_input_source(param_name: str) -> str:
    """Deterministic fallback used to validate/backfill the AI's own
    proposed input_source for a parameter — never overrides a
    plausible AI suggestion, only fills gaps."""
    if _SECRET_NAME_RE.search(param_name or ""):
        return "secret_configuration"
    if _PROFILE_NAME_RE.match((param_name or "").strip()):
        return "customer_profile"
    return "customer_message"


def suggest_secret_ref(param_name: str, action_key: str) -> str:
    """Deterministic environment-variable-name suggestion for a detected
    secret parameter — never a real value, just a name the admin can
    accept or edit."""
    base = re.sub(r"[^A-Za-z0-9]+", "_", f"{action_key}_{param_name}").strip("_").upper()
    return f"{base}_SECRET" if not base.endswith("SECRET") else base


# ── Structured proposal schema ───────────────────────────────────────────

REQUIRED_TOP_LEVEL_KEYS = (
    "action_name", "display_name", "action_id", "description", "category", "action_type",
    "http_method", "base_url", "endpoint_path", "content_type", "headers", "parameters",
    "parameter_groups", "keywords", "example_questions", "response_mapping",
    "customer_facing_response_template",
)
REQUIRED_PARAMETER_KEYS = (
    "name", "display_name", "required", "input_source", "example_value",
    "validation_type", "validation_pattern", "validation_confidence", "follow_up_options",
)


# ── Smart Capability Setup — type detection (additive; all keys below
# are OPTIONAL on the proposal so every pre-existing AI Auto Setup
# proposal/test fixture that predates this feature remains valid and
# passes validate_proposal_schema() unchanged) ───────────────────────
DETECTION_CONFIDENCE_LEVELS = ("high", "medium", "low")
OPTIONAL_SMART_SETUP_KEYS = (
    "detected_action_type", "detection_confidence", "detection_reason",
    "type_interpretations", "clarification_question",
    "conditions", "warnings", "connected_system", "knowledge_scope",
    "tool_name", "triggers", "workflow_steps", "routing_recommendation",
    "business_description", "success_prompt", "failure_prompt", "follow_up_prompt",
    "when_not_to_call", "rag_combination", "confidence_threshold_recommendation",
)

# RAG-vs-ERP Routing Configuration (routing metadata, not wired into the
# Decision Engine yet — see setup_metadata.routing on the saved action).
ROUTING_SOURCE_OPTIONS = (
    "erp_only", "kb_only", "erp_then_kb", "kb_then_erp", "both_combine", "decision_engine_choice",
)
RAG_COMBINATION_OPTIONS = (
    "no_rag", "rag_explanation_only", "combine_with_kb", "fallback_to_rag",
)


def validate_proposal_schema(data: Dict) -> Tuple[bool, List[str]]:
    """Server-side structural validation of the AI's JSON response —
    the AI's output is NEVER trusted or auto-saved without this check
    passing first."""
    errors = []
    if not isinstance(data, dict):
        return False, ["response is not a JSON object"]
    for key in REQUIRED_TOP_LEVEL_KEYS:
        if key not in data:
            errors.append(f"missing top-level key: {key}")
    if data.get("action_type") and data["action_type"] not in ACTION_TYPES:
        errors.append(f"invalid action_type: {data.get('action_type')}")
    if data.get("detected_action_type") and data["detected_action_type"] not in ACTION_TYPES:
        errors.append(f"invalid detected_action_type: {data.get('detected_action_type')}")
    if data.get("detection_confidence") and data["detection_confidence"] not in DETECTION_CONFIDENCE_LEVELS:
        errors.append(f"invalid detection_confidence: {data.get('detection_confidence')}")
    if data.get("type_interpretations") is not None and not isinstance(data["type_interpretations"], list):
        errors.append("type_interpretations must be a list")
    if data.get("http_method") and data["http_method"] not in HTTP_METHODS:
        errors.append(f"invalid http_method: {data.get('http_method')}")
    parameters = data.get("parameters")
    if not isinstance(parameters, list):
        errors.append("parameters must be a list")
        parameters = []
    for i, p in enumerate(parameters):
        if not isinstance(p, dict):
            errors.append(f"parameters[{i}] is not an object")
            continue
        for key in REQUIRED_PARAMETER_KEYS:
            if key not in p:
                errors.append(f"parameters[{i}] missing key: {key}")
        if p.get("input_source") and p["input_source"] not in INPUT_SOURCES:
            errors.append(f"parameters[{i}] invalid input_source: {p.get('input_source')}")
        if p.get("input_source") == "secret_configuration" and not p.get("secret_ref"):
            errors.append(f"parameters[{i}] ({p.get('name')}) is secret_configuration but has no secret_ref")
        if p.get("input_source") == "credential_store" and not p.get("credential_ref"):
            errors.append(f"parameters[{i}] ({p.get('name')}) is credential_store but has no credential_ref")
        if p.get("validation_confidence") not in (None, "high", "medium", "low"):
            errors.append(f"parameters[{i}] invalid validation_confidence: {p.get('validation_confidence')}")
    groups = data.get("parameter_groups")
    if groups is not None:
        if not isinstance(groups, list):
            errors.append("parameter_groups must be a list")
        else:
            for g in groups:
                if g.get("rule") and g["rule"] not in GROUP_RULES:
                    errors.append(f"parameter_groups invalid rule: {g.get('rule')}")
    return (len(errors) == 0), errors


_SYSTEM_PROMPT = """คุณคือผู้ช่วยตั้งค่า Business Action สำหรับแพลตฟอร์ม AI Customer Service
หน้าที่ของคุณคือวิเคราะห์สิ่งที่ผู้ดูแลระบบให้มา (ลิงก์ API, cURL, เอกสาร, ไฟล์ตั้งค่า หรือคำอธิบาย
ภาษาธรรมดา) แล้วเสนอโครงสร้าง configuration ในรูปแบบ JSON ที่เคร่งครัดตาม schema ที่กำหนด ห้ามเดา
secret value ใดๆ (ค่า secret ถูกลบออกไปแล้วก่อนส่งถึงคุณ — ให้เสนอเพียงชื่อ environment variable
ที่ควรใช้เท่านั้น)

ก่อนอื่น ให้ตรวจสอบว่าผู้ใช้ต้องการความสามารถประเภทใด (detected_action_type) จาก 7 ประเภทนี้เท่านั้น:
- API: มี HTTP endpoint, method, cURL, หรือ request body ชัดเจน และเป็นการเรียก API ทั่วไป
- WEBHOOK: ต้องการรับข้อมูล (incoming) จากระบบภายนอก ไม่ใช่การค้นหาข้อมูล
- RAG: ต้องการค้นหาคำตอบจากเอกสาร/Knowledge Base
- TOOL: ต้องการคำนวณ แปลง หรือประมวลผลภายในระบบ (ไม่เรียก API ภายนอก)
- HUMAN_HANDOFF: ต้องการส่งต่อให้เจ้าหน้าที่
- NOTIFICATION: ต้องการแจ้งเตือนหรือส่งข้อมูลออกไป
- WORKFLOW: อธิบายหลายขั้นตอนต่อเนื่องกัน

ให้ประเมิน detection_confidence เป็น "high" (มั่นใจมาก ไม่ต้องถามซ้ำ), "medium" (เป็นไปได้หลายแบบ —
เสนอ type_interpretations 2-3 ทางเลือกแบบเข้าใจง่ายสำหรับผู้ใช้ทั่วไป ไม่ใช่ชื่อ action_type ดิบๆ), หรือ
"low" (ไม่ชัดเจนพอ — ตั้ง clarification_question เป็นคำถามสั้นๆ หนึ่งข้อเพื่อถามผู้ใช้เพิ่ม)

กติกาสำคัญ (สำหรับ API/WEBHOOK):
- ถ้าพารามิเตอร์ดูเหมือนเป็นค่าลับ (SecretCode, Authorization, ApiKey, Token, ClientSecret, Password)
  ให้ตั้ง input_source เป็น "secret_configuration" และเสนอ secret_ref เป็นชื่อ environment variable
  (ห้ามใส่ค่าจริง) — ห้ามถามลูกค้า
- ถ้าพารามิเตอร์หลายตัวเป็นทางเลือก (เช่น CustCode/CustEmail/CustName/CustPhone อย่างใดอย่างหนึ่งพอ)
  ให้จัดกลุ่มแบบ AT_LEAST_ONE — ถ้าทุกตัวจำเป็นต้องมีพร้อมกัน (เช่น CustCode และ OrderCode)
  ห้ามสร้างกลุ่ม ให้แต่ละตัว required=true แยกกัน
- สำหรับพารามิเตอร์ที่ต้องถามลูกค้าและอาจสับสนกับพารามิเตอร์อื่นที่มีรูปแบบคล้ายกัน (เช่น C00001 vs
  PO202601001) ให้เสนอ validation_pattern (regex) ที่แยกความแตกต่างได้ชัดเจน ถ้าไม่มั่นใจ ให้ตั้ง
  validation_confidence เป็น "low" และอย่าแสร้งว่ามั่นใจ
- เสนอ follow_up_options อย่างน้อย 3 ข้อความภาษาไทยธรรมชาติต่อพารามิเตอร์ที่ต้องถามลูกค้อง

สำหรับ RAG: ระบุ knowledge_scope (หัวข้อ/ขอบเขตเอกสารที่ควรค้นหา) แทน parameters
สำหรับ TOOL: ระบุ tool_name (ชื่อ internal tool ที่มีอยู่แล้ว เช่น "calculator", "url_converter")
สำหรับ HUMAN_HANDOFF/NOTIFICATION: ระบุ triggers (รายการเงื่อนไขที่ควรทำงาน)
สำหรับ WORKFLOW: ระบุ workflow_steps (รายการขั้นตอนตามลำดับ)
เติม conditions (เงื่อนไขการทำงาน) และ warnings (คำเตือนที่ควรตรวจสอบ เช่น ข้อมูลที่ไม่แน่ใจ) เสมอ
(ใช้ list ว่างถ้าไม่มี) และ connected_system (ชื่อระบบ/แหล่งข้อมูลที่เชื่อมต่อ เช่นชื่อ API หรือ
"Knowledge Base")

ให้เสนอ routing_recommendation หนึ่งค่าจาก: "erp_only" (ค่าเริ่มต้นที่แนะนำสำหรับ API/WEBHOOK ส่วนใหญ่),
"erp_then_kb" (ถ้าอาจต้องเสริมด้วยนโยบาย/คำอธิบายจาก Knowledge Base), "kb_only" (สำหรับ RAG),
"kb_then_erp", "both_combine", หรือ "decision_engine_choice" — นี่เป็นเพียงคำแนะนำเริ่มต้น
ผู้ดูแลระบบจะแก้ไขเองได้ในภายหลัง ไม่ได้ใช้ควบคุมการทำงานจริงในตอนนี้

ห้ามปล่อยฟิลด์ต่อไปนี้ว่างเปล่าเมื่อมีข้อมูล API เพียงพอที่จะเขียนเนื้อหาที่เป็นประโยชน์จริง — ใช้ภาษาเดียวกับ
Business Purpose ที่ผู้ดูแลระบบให้มา (ปกติคือภาษาไทย):
- business_description: อธิบายว่า action นี้ดึงข้อมูลอะไรจากระบบไหน เป็นภาษาธุรกิจที่ไม่ใช่ศัพท์เทคนิค
- success_prompt: คำแนะนำสำหรับการนำเสนอผลลัพธ์ที่สำเร็จให้ลูกค้า (แสดงเฉพาะข้อมูลที่เกี่ยวข้อง ห้ามเปิดเผย
  credential หรือ raw API payload)
- failure_prompt: คำแนะนำเมื่อเรียกไม่สำเร็จหรือไม่พบข้อมูล (ห้ามกุข้อมูลขึ้นมาเอง ให้แนะนำตรวจสอบข้อมูลหรือส่งต่อเจ้าหน้าที่)
- follow_up_prompt: ควรถามอะไรลูกค้าเมื่อข้อมูลที่ต้องใช้ค้นหายังไม่ครบ
- when_not_to_call: สถานการณ์ที่ไม่ควรเรียก action นี้ (เช่น คำถามทั่วไปเชิงนโยบาย/เอกสารที่ไม่ต้องใช้ข้อมูลเฉพาะราย)
- rag_combination: หนึ่งค่าจาก "no_rag"/"rag_explanation_only"/"combine_with_kb"/"fallback_to_rag"
- confidence_threshold_recommendation: ตัวเลข 0-1 (คำแนะนำเริ่มต้น แก้ไขได้ภายหลัง)

ห้ามจัดประเภทพารามิเตอร์ที่เป็นค่าลับ (เช่น SecretCode, ApiKey, Token, ClientSecret) เป็นข้อมูลที่ต้องถามลูกค้า
(customer_message) โดยเด็ดขาด — ต้องเป็น secret_configuration หรือ credential_store เท่านั้น และห้ามย้ายค่าลับ
ที่ถูกส่งมาแบบ form/body ไปไว้ใน headers เว้นแต่เอกสาร API จะระบุไว้ชัดเจนว่าต้องส่งเป็น header

ตอบเป็น JSON เท่านั้น ห้ามมีข้อความอื่นนอก JSON ห้ามใช้ ```"""


def _build_user_prompt(api_input_redacted: str, business_purpose: str, example_questions: List[str],
                        search_info: str = "") -> str:
    schema_hint = {
        "action_name": "string", "display_name": "string", "action_id": "string (slug)",
        "description": "string", "category": "string", "action_type": "API",
        "detected_action_type": "API|WEBHOOK|RAG|TOOL|HUMAN_HANDOFF|NOTIFICATION|WORKFLOW",
        "detection_confidence": "high|medium|low", "detection_reason": "string",
        "type_interpretations": [{"label": "business-friendly Thai sentence", "action_type": "API"}],
        "clarification_question": "string or null",
        "http_method": "GET|POST|PUT|PATCH|DELETE", "base_url": "string", "endpoint_path": "string",
        "content_type": "application/json|application/x-www-form-urlencoded",
        "headers": {"Header-Name": "value"},
        "parameters": [{
            "name": "string", "display_name": "string", "required": True,
            "input_source": "customer_message|customer_profile|conversation_context|"
                             "fixed_configuration|secret_configuration|system_generated",
            "secret_ref": "ENV_VAR_NAME or null", "example_value": "string",
            "validation_type": "email|phone_number|non_empty|regex or null",
            "validation_pattern": "regex string or null",
            "validation_confidence": "high|medium|low",
            "follow_up_options": ["option 1", "option 2", "option 3"],
        }],
        "parameter_groups": [{"name": "string", "rule": "AT_LEAST_ONE", "members": ["param_name", "..."]}],
        "keywords": ["string"], "example_questions": ["string"],
        "response_mapping": [{"json_path": "$.field", "mapped_label": "string"}],
        "customer_facing_response_template": "string",
        "conditions": ["string"], "warnings": ["string"], "connected_system": "string",
        "knowledge_scope": "string or null", "tool_name": "string or null",
        "triggers": ["string"], "workflow_steps": ["string"],
        "routing_recommendation": "erp_only|kb_only|erp_then_kb|kb_then_erp|both_combine|decision_engine_choice",
        # AI Behaviour tab auto-completion (never left blank when there's
        # enough API context to write something concrete and useful).
        "business_description": "string — what this action retrieves/does and from where, in plain business language",
        "success_prompt": "string — instructions for presenting a SUCCESSFUL result to the customer",
        "failure_prompt": "string — instructions for what to say when the call fails or returns nothing",
        "follow_up_prompt": "string — what to ask the customer when required search information is missing",
        # Routing tab auto-completion.
        "when_not_to_call": "string — situations where this action should NOT be used",
        "rag_combination": "no_rag|rag_explanation_only|combine_with_kb|fallback_to_rag",
        "confidence_threshold_recommendation": "number between 0 and 1",
    }
    search_info_line = f"ข้อมูลที่ต้องใช้ค้นหา (ระบุโดยผู้ดูแลระบบ): {search_info}\n\n" if search_info else ""
    return (
        f"ใช้สำหรับ (Business Purpose): {business_purpose}\n\n"
        f"{search_info_line}"
        f"ตัวอย่างคำถามลูกค้า:\n" + "\n".join(f"- {q}" for q in example_questions) + "\n\n"
        f"Input จากผู้ใช้ (secret ถูกลบแล้ว):\n{api_input_redacted}\n\n"
        f"โปรดตอบเป็น JSON ตาม schema นี้เท่านั้น:\n{json.dumps(schema_hint, ensure_ascii=False, indent=2)}"
    )


def _parse_json_response(text: str) -> Dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    return json.loads(text)


# ══════════════════════════════════════════════════════════════════════
# Structured AI Analysis Pipeline (AI Intelligence sprint)
#
#   API Input -> Parser -> Schema Extraction (LLM) -> Parameter
#   Analysis -> Business Mapping -> Response Mapping -> Intent
#   Classification -> Similarity Analysis -> Confidence Calculation ->
#   AI Self Review -> Business Action Draft
#
# "Parser" = detect_and_redact_secrets()/detect_endpoints_from_document()
# (already existed). "Schema Extraction" = the single LLM call in
# analyze_capability(). Everything below is the NEW, deterministic,
# rule-based reasoning layer built on top of that one LLM call's raw
# output — no additional LLM calls, no prompt-generated blobs. Every
# stage returns its own small, structured dict so a caller can inspect
# exactly what each stage decided and why.
# ══════════════════════════════════════════════════════════════════════

# ── Part 3: Parameter Analysis Engine — infers a semantic TYPE for a
# parameter from its name + example value. Deterministic, name/value
# pattern based (never a guess dressed up as a fact).
_TYPE_PATTERNS = [
    ("identifier", re.compile(r"(code|id|number|no)$", re.IGNORECASE)),
    ("email", re.compile(r"email|e-mail", re.IGNORECASE)),
    ("phone", re.compile(r"phone|tel|mobile|เบอร์", re.IGNORECASE)),
    ("date", re.compile(r"date|_at$|_on$|timestamp", re.IGNORECASE)),
    ("money", re.compile(r"amount|price|balance|wallet|total|fee|cost", re.IGNORECASE)),
    ("status", re.compile(r"status|state|flag$", re.IGNORECASE)),
    ("boolean", re.compile(r"^is_|^has_|enabled|active$", re.IGNORECASE)),
]


def _infer_param_type(name: str, example_value) -> str:
    name = name or ""
    if isinstance(example_value, bool):
        return "boolean"
    if isinstance(example_value, list):
        return "array"
    if isinstance(example_value, dict):
        return "object"
    for type_name, pattern in _TYPE_PATTERNS:
        if pattern.search(name):
            return type_name
    if isinstance(example_value, str):
        if re.match(r"^[\w.+-]+@[\w-]+\.[\w.-]+$", example_value):
            return "email"
        if re.match(r"^0\d{8,9}$", example_value):
            return "phone"
        if re.match(r"^\d{4}-\d{2}-\d{2}", example_value):
            return "date"
    return "string"


# Humanizes a raw technical parameter/field name into a business label,
# e.g. "CustCode" -> "Cust Code" -> title-cased "Cust Code". Falls back
# to the name itself (never invents a label out of nothing).
_HUMANIZE_ABBR = {
    "cust": "Customer", "qty": "Quantity", "amt": "Amount", "no": "Number",
    "addr": "Address", "desc": "Description", "id": "ID", "tel": "Phone",
}


def _humanize_name(name: str) -> str:
    if not name:
        return ""
    # snake_case / camelCase / PascalCase -> space-separated words
    s = re.sub(r"(?<!^)(?=[A-Z])", " ", name)
    s = s.replace("_", " ").replace("-", " ")
    words = [w for w in s.split() if w]
    out = []
    for w in words:
        low = w.lower()
        out.append(_HUMANIZE_ABBR.get(low, w.capitalize()))
    return " ".join(out) or name


def analyze_parameter(param: Dict) -> Dict:
    """Part 3 — Parameter Analysis Engine. Infers purpose/type/nullable/
    business meaning for ONE parameter dict from the AI's proposal.
    Never mutates the input; returns a new structured analysis dict."""
    name = param.get("name") or ""
    example_value = param.get("example_value")
    inferred_type = _infer_param_type(name, example_value)
    required = bool(param.get("required"))
    return {
        "name": name,
        "type": inferred_type,
        "nullable": not required,
        "required": required,
        "validation": param.get("validation_type") or ("regex" if param.get("validation_pattern") else "string"),
        "business_meaning": _humanize_name(name),
        "example": example_value,
    }


# ── Part 1: Business Field Mapping — separates business language from
# technical language for every input parameter. "Needs Review" instead
# of guessing when the AI gave us nothing to build a confident label
# from (empty/placeholder display_name AND an un-humanizable name).
def build_business_mapping(proposal: Dict) -> List[Dict]:
    groups_by_member = {}
    for g in proposal.get("parameter_groups") or []:
        for m in g.get("members") or []:
            groups_by_member[m] = g.get("rule")

    out = []
    for param in proposal.get("parameters") or []:
        if not isinstance(param, dict):
            continue
        name = param.get("name") or ""
        analysis = analyze_parameter(param)
        display_name = (param.get("display_name") or "").strip()
        business_field = display_name or analysis["business_meaning"]
        needs_review = not display_name and not re.search(r"[A-Za-z]", name)
        out.append({
            "technical_parameter": name,           # NEVER lost — always the original key
            "business_field": business_field if not needs_review else "Needs Review",
            "description": param.get("description") or f"{business_field} value" if not needs_review else None,
            "example": param.get("example_value"),
            "validation": analysis["validation"],
            "type": analysis["type"],
            "required_rule": groups_by_member.get(name, "ALL_REQUIRED" if param.get("required") else "OPTIONAL"),
            "needs_review": needs_review,
        })
    return out


# ── Part 2: Response Field Mapping — same business/technical split for
# the API's OWN response_mapping entries (json_path -> mapped_label).
def build_response_field_mapping(proposal: Dict) -> List[Dict]:
    out = []
    for m in proposal.get("response_mapping") or []:
        if not isinstance(m, dict):
            continue
        json_path = m.get("json_path") or ""
        field_name = json_path.split(".")[-1].lstrip("$").strip() or json_path
        mapped_label = (m.get("mapped_label") or "").strip()
        needs_review = not mapped_label
        out.append({
            "api_field": json_path,
            "business_name": mapped_label or _humanize_name(field_name) or "Needs Review",
            "type": _infer_param_type(field_name, None),
            "needs_review": needs_review,
        })
    return out


# ── Part 5: Intent Classification — a fixed business taxonomy, matched
# by keyword/category heuristics (never invents a category outside this
# list; falls back to "unknown" rather than guessing confidently wrong).
_INTENT_TAXONOMY = [
    ("customer_lookup", "Customer Lookup", [r"customer.*(look|get|search|find)", r"ลูกค้า.*(ค้นหา|ดึง)"]),
    ("customer_update", "Customer Update", [r"customer.*(update|edit|change)", r"แก้ไข.*ลูกค้า"]),
    ("order_lookup", "Order Lookup", [r"order.*(look|get|search|status)", r"ออเดอร์|คำสั่งซื้อ"]),
    ("order_update", "Order Update", [r"order.*(update|cancel|edit)"]),
    ("shipment", "Shipment", [r"shipment|delivery|จัดส่ง"]),
    ("tracking", "Tracking", [r"track", r"ติดตาม|พัสดุ"]),
    ("finance", "Finance", [r"wallet|balance|invoice|refund|payment|finance"]),
    ("authentication", "Authentication", [r"login|auth|token|otp"]),
    ("notification", "Notification", [r"notify|notification|alert|แจ้งเตือน"]),
    ("report", "Report", [r"report|summary|analytics"]),
    ("file_upload", "File Upload", [r"upload|file"]),
    ("ocr", "OCR", [r"\bocr\b"]),
    ("vision", "Vision", [r"vision|image.*(analy|detect)"]),
]

_SYNONYM_TABLE = {
    "customer": ["ลูกค้า", "client", "member"], "order": ["ออเดอร์", "คำสั่งซื้อ", "po"],
    "tracking": ["ติดตาม", "พัสดุ", "shipment"], "wallet": ["เงิน", "ยอดเงิน", "balance"],
}


def classify_intent(proposal: Dict) -> Dict:
    """Part 5 — Intent Classification. Matches the proposal's own
    description/display_name/category/keywords against a fixed taxonomy.
    Never returns a category outside _INTENT_TAXONOMY plus "unknown"."""
    haystack = " ".join([
        proposal.get("description") or "", proposal.get("display_name") or "",
        proposal.get("category") or "", " ".join(proposal.get("keywords") or []),
    ]).lower()

    for intent_id, display_name, patterns in _INTENT_TAXONOMY:
        if any(re.search(p, haystack, re.IGNORECASE) for p in patterns):
            keywords = proposal.get("keywords") or [proposal.get("category") or intent_id]
            synonyms = sorted(set(sum((_SYNONYM_TABLE.get(k.lower(), []) for k in keywords), [])))
            return {
                "intent_id": intent_id, "intent_display_name": display_name,
                "intent_description": proposal.get("description") or display_name,
                "example_user_questions": proposal.get("example_questions") or [],
                "keywords": keywords, "synonyms": synonyms,
            }
    return {
        "intent_id": "unknown", "intent_display_name": "Unknown",
        "intent_description": proposal.get("description") or "Could not confidently classify this capability.",
        "example_user_questions": proposal.get("example_questions") or [],
        "keywords": proposal.get("keywords") or [], "synonyms": [],
    }


# ── Part 6: Similarity Engine — compares the new proposal against every
# EXISTING Business Action (caller supplies the list, e.g. registry.list())
# so a duplicate is recommended for reuse/merge instead of silently
# created again.
def _token_set(*texts) -> set:
    words = set()
    for t in texts:
        if not t:
            continue
        words.update(re.findall(r"[a-zA-Z฀-๿]+", str(t).lower()))
    return words


def compute_similarity(proposal: Dict, existing_actions: List[Dict]) -> Dict:
    """Part 6 — never silently duplicates. Returns the best-matching
    existing action (if any) with per-dimension similarity scores and a
    recommendation: reuse_existing / merge / replace / create_new."""
    proposal_tokens = _token_set(proposal.get("display_name"), proposal.get("description"),
                                  proposal.get("category"), " ".join(proposal.get("keywords") or []))
    proposal_param_names = {p.get("name", "").lower() for p in (proposal.get("parameters") or [])}
    endpoint = (proposal.get("endpoint_path") or "").lower()

    best = None
    for action in existing_actions or []:
        existing_tokens = _token_set(action.get("display_name"), action.get("name"), action.get("category"))
        semantic = len(proposal_tokens & existing_tokens) / max(1, len(proposal_tokens | existing_tokens))

        existing_params = {p.get("name", "").lower() for p in (action.get("parameters") or [])}
        param_sim = (len(proposal_param_names & existing_params) / max(1, len(proposal_param_names | existing_params))
                     if (proposal_param_names or existing_params) else 0.0)

        existing_endpoint = (action.get("execution_host") or action.get("endpoint") or "").lower()
        endpoint_sim = 1.0 if endpoint and existing_endpoint and endpoint in existing_endpoint else \
            (0.5 if endpoint and existing_endpoint and endpoint.split("/")[-1] in existing_endpoint else 0.0)

        capability_sim = 1.0 if (action.get("category") and action.get("category") == proposal.get("category")) else 0.0

        overall = round(0.4 * semantic + 0.25 * param_sim + 0.2 * endpoint_sim + 0.15 * capability_sim, 3)
        if best is None or overall > best["overall_similarity"]:
            best = {
                "action_id": action.get("id"), "action_key": action.get("action_key"),
                "display_name": action.get("display_name") or action.get("name"),
                "semantic_similarity": round(semantic, 3), "parameter_similarity": round(param_sim, 3),
                "endpoint_similarity": round(endpoint_sim, 3), "capability_similarity": round(capability_sim, 3),
                "overall_similarity": overall,
            }

    if best is None:
        return {"match": None, "recommendation": "create_new"}
    if best["overall_similarity"] >= 0.85:
        recommendation = "merge"
    elif best["overall_similarity"] >= 0.6:
        recommendation = "reuse_existing"
    elif best["overall_similarity"] >= 0.4:
        recommendation = "replace"
    else:
        recommendation = "create_new"
    best["recommendation"] = recommendation
    return {"match": best, "recommendation": recommendation}


# ── Part 4: Confidence Engine — five equally-weighted (20% each)
# signals instead of one static high/medium/low guess. Each sub-score
# is itself a simple, explainable completeness check — never a second
# LLM opinion about its own output.
def compute_confidence(proposal: Dict, business_mapping: List[Dict],
                        response_field_mapping: List[Dict], intent: Dict) -> Dict:
    def score_api_structure():
        required = ["http_method", "base_url", "endpoint_path"]
        present = sum(1 for k in required if proposal.get(k))
        return present / len(required)

    def score_search_fields():
        askable = [p for p in (proposal.get("parameters") or [])
                   if p.get("required") and p.get("input_source") not in ("secret_configuration", "credential_store")]
        return 1.0 if askable else 0.0

    def score_response_mapping():
        mapped = [m for m in response_field_mapping if not m["needs_review"]]
        if not response_field_mapping:
            return 0.0
        return len(mapped) / len(response_field_mapping)

    def score_business_mapping():
        if not business_mapping:
            return 0.0
        clean = [m for m in business_mapping if not m["needs_review"]]
        return len(clean) / len(business_mapping)

    def score_routing():
        return 1.0 if proposal.get("routing_recommendation") else 0.5

    per_section = {
        "api_structure": round(score_api_structure(), 3),
        "search_field_detection": round(score_search_fields(), 3),
        "response_mapping": round(score_response_mapping(), 3),
        "business_mapping": round(score_business_mapping(), 3),
        "routing_recommendation": round(score_routing(), 3),
    }
    overall = round(sum(per_section.values()) / len(per_section), 3)
    return {
        "overall_score": overall,
        "overall_percent": f"{round(overall * 100)}%",
        "per_section": per_section,
        "needs_review": overall < 0.5,
    }


# ── Part 9: Mock Data Generator — realistic sample request VALUES
# derived purely from each parameter's inferred type (Part 3). No
# hardcoded single example reused everywhere.
_MOCK_BY_TYPE = {
    "identifier": lambda n: "C" + str(abs(hash(n)) % 900000 + 100000),
    "email": lambda n: "customer@example.com",
    "phone": lambda n: "08" + str(abs(hash(n)) % 90000000 + 10000000),
    "date": lambda n: "2026-01-15",
    "money": lambda n: "1250.00",
    "status": lambda n: "active",
    "boolean": lambda n: True,
    "array": lambda n: [],
    "object": lambda n: {},
    "string": lambda n: (n or "value").strip().lower().replace(" ", "_") or "sample",
}


def generate_mock_data(proposal: Dict) -> Dict:
    """Part 9 — one realistic mock value per parameter, keyed by the
    ORIGINAL technical parameter name (never a business label) so it
    can be fed straight into the existing Test API flow unchanged."""
    mock = {}
    for param in proposal.get("parameters") or []:
        if not isinstance(param, dict):
            continue
        name = param.get("name") or ""
        if param.get("example_value") not in (None, ""):
            mock[name] = param["example_value"]
            continue
        inferred_type = _infer_param_type(name, None)
        mock[name] = _MOCK_BY_TYPE.get(inferred_type, _MOCK_BY_TYPE["string"])(name)
    return mock


# ── Part 8: Response Preview — business view + technical view of what
# a sample response would look like, built from response_field_mapping
# + a generated mock value per field (same type-inference as Part 9).
def generate_response_preview(proposal: Dict, mock_data: Dict) -> Dict:
    business_view = []
    technical_view = []
    for m in proposal.get("response_mapping") or []:
        if not isinstance(m, dict):
            continue
        json_path = m.get("json_path") or ""
        field_name = json_path.split(".")[-1].lstrip("$").strip() or json_path
        mapped_label = (m.get("mapped_label") or _humanize_name(field_name) or field_name)
        inferred_type = _infer_param_type(field_name, None)
        value = _MOCK_BY_TYPE.get(inferred_type, _MOCK_BY_TYPE["string"])(field_name)
        business_view.append({"label": mapped_label, "value": value})
        technical_view.append({"json_path": json_path, "value": value})
    return {"business_view": business_view, "technical_view": technical_view}


# ── Part 10: AI Self Review — a checklist the AI runs on its OWN output
# before handing the draft to the admin. Anything uncertain is flagged
# "needs_review" rather than silently assumed complete.
def run_self_review(proposal: Dict, business_mapping: List[Dict], response_field_mapping: List[Dict],
                     confidence: Dict, similarity: Dict, response_preview: Dict) -> Dict:
    has_auth_signal = proposal.get("auth_type") not in (None, "", "none") or any(
        p.get("input_source") in ("secret_configuration", "credential_store") for p in (proposal.get("parameters") or []))
    checklist = {
        "business_mapping_complete": bool(business_mapping) and not any(m["needs_review"] for m in business_mapping),
        "response_mapping_complete": bool(response_field_mapping) and not any(m["needs_review"] for m in response_field_mapping),
        "search_fields_detected": confidence["per_section"]["search_field_detection"] >= 1.0,
        "routing_selected": bool(proposal.get("routing_recommendation")),
        "authentication_detected": has_auth_signal,
        "duplicate_checked": similarity.get("recommendation") is not None,
        "response_preview_generated": bool(response_preview.get("business_view")),
    }
    all_clear = all(checklist.values())
    return {
        "checklist": checklist,
        "all_clear": all_clear,
        "status": "ready" if (all_clear and not confidence["needs_review"]) else "needs_review",
    }


def analyze_capability(user_input: str, business_purpose: str, example_questions: List[str], *,
                        search_info: str = "", existing_actions: Optional[List[Dict]] = None,
                        model: str = "gpt-4o-mini", max_tokens: int = 1500) -> Dict:
    """Smart Capability Setup's single analysis entry point — accepts a
    link/cURL/document/plain-language description, redacts secrets,
    and asks the AI to BOTH detect the capability type (API/RAG/TOOL/
    HUMAN_HANDOFF/NOTIFICATION/WORKFLOW/WEBHOOK) and propose a full
    structured configuration, never requiring the admin to pick a type
    up front. Returns {"ok": bool, "proposal": dict|None, "errors": [...],
    "redacted_secrets_found": [...], "raw_text": str}. Never raises for
    a malformed/invalid AI response — the caller (admin route) surfaces
    `ok=False`/`errors` to the UI instead of a 500."""
    redacted_input, detected_credentials = detect_and_redact_secrets(user_input)
    redacted_secrets = [c["name"] for c in detected_credentials]
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt(redacted_input, business_purpose, example_questions or [], search_info or "")},
    ]
    llm = get_llm_service()
    llm_response = llm.generate(messages, model=model, temperature=0.2, max_tokens=max_tokens)

    try:
        proposal = _parse_json_response(llm_response.text)
    except (json.JSONDecodeError, ValueError) as e:
        return {"ok": False, "proposal": None, "errors": [f"AI response was not valid JSON: {e}"],
                "redacted_secrets_found": redacted_secrets, "detected_credentials": detected_credentials,
                "raw_text": llm_response.text}

    # Parameter defaulting/mutation happens BEFORE schema validation —
    # the raw AI response is allowed to leave input_source/secret_ref
    # unset for a detected secret (that's exactly what our own local
    # detector is for); validating the FINAL, defaulted shape is what
    # actually reflects what gets saved.
    detected_by_name = {c["name"].lower(): c for c in detected_credentials}
    for p in proposal.get("parameters", []) if isinstance(proposal.get("parameters"), list) else []:
        if not isinstance(p, dict):
            continue
        if not p.get("input_source"):
            p["input_source"] = infer_input_source(p.get("name", ""))
        # New Smart Setup proposals default secret-shaped parameters to
        # credential_store (Paste-and-Use), never a per-API .env
        # variable — legacy secret_configuration remains supported for
        # existing actions/tests, just no longer the default for NEW ones.
        matched = detected_by_name.get((p.get("name") or "").lower())
        if matched and p.get("input_source") in ("secret_configuration", "credential_store"):
            p["input_source"] = "credential_store"
            p.setdefault("credential_ref", matched["suggested_credential_key"])
            p["secret_ref"] = None
            p["_detected_credential"] = matched  # UI-only hint; stripped before saving to the Registry

    ok, errors = validate_proposal_schema(proposal)
    if ok and not proposal.get("detected_action_type"):
        proposal["detected_action_type"] = proposal.get("action_type")
    if ok and not proposal.get("detection_confidence"):
        proposal["detection_confidence"] = "high"

    result = {"ok": ok, "proposal": proposal if ok else proposal, "errors": errors,
              "redacted_secrets_found": redacted_secrets, "detected_credentials": detected_credentials,
              "raw_text": llm_response.text}

    # ── Structured Analysis Pipeline (Parts 1-10) — deterministic,
    # rule-based post-processing of the AI's raw extraction. Every
    # stage below produces its own structured intermediate data
    # (never a second giant prompt-generated blob) and is additive —
    # none of it changes `proposal`'s own schema/keys, so existing
    # callers (Business Action Registry, the save endpoint, the UI)
    # are completely unaffected if they never read these new keys.
    if ok:
        business_mapping = build_business_mapping(proposal)
        response_field_mapping = build_response_field_mapping(proposal)
        intent = classify_intent(proposal)
        similarity = compute_similarity(proposal, existing_actions or [])
        confidence = compute_confidence(proposal, business_mapping, response_field_mapping, intent)
        mock_data = generate_mock_data(proposal)
        response_preview = generate_response_preview(proposal, mock_data)
        self_review = run_self_review(proposal, business_mapping, response_field_mapping,
                                       confidence, similarity, response_preview)
        result.update({
            "business_mapping": business_mapping,
            "response_field_mapping": response_field_mapping,
            "intent_classification": intent,
            "similarity": similarity,
            "confidence": confidence,
            "mock_data": mock_data,
            "response_preview": response_preview,
            "self_review": self_review,
        })
        # Deliberately NOT overwriting proposal["detection_confidence"]
        # here — that string still drives the existing Stage 1b
        # clarification/interpretation UI flow (unchanged this sprint;
        # "do not redesign the UI"). The new weighted `confidence` dict
        # above is purely additive — a caller that wants the richer,
        # multi-signal view reads `result["confidence"]` instead.

    return result


# Backward-compatible alias — existing AI Auto Setup callers/tests call
# analyze_api() with the exact same signature; Smart Capability Setup
# is a strict superset of that behavior (adds type detection fields),
# so the same implementation serves both entry points unchanged.
def analyze_api(api_input: str, business_purpose: str, example_questions: List[str], *,
                 model: str = "gpt-4o-mini", max_tokens: int = 1500) -> Dict:
    return analyze_capability(api_input, business_purpose, example_questions, model=model, max_tokens=max_tokens)


# ── Postman Collection / OpenAPI multi-endpoint detection ────────────────
# Pure local parsing, no LLM call — lets the wizard show a selectable
# endpoint list BEFORE spending an LLM call per endpoint. Any secret
# values in headers/urls are redacted before anything is returned to
# the browser.

_CATEGORY_KEYWORDS = [
    ("customer", "Customer lookup"), ("order", "Order lookup"),
    ("shipment", "Shipment lookup"), ("tracking", "Tracking lookup"),
    ("image", "Product image"), ("line", "LINE OA notification"),
]


def _guess_endpoint_category(name: str, url: str) -> str:
    text = f"{name} {url}".lower()
    is_filtered = "filter" in text or "search" in text
    for kw, label in _CATEGORY_KEYWORDS:
        if kw in text:
            if is_filtered and kw in ("order", "shipment"):
                return f"Filtered {kw} search"
            return label
    return "Uncategorized"


def _walk_postman_items(items: List[Dict], out: List[Dict]) -> None:
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("item"), list):
            _walk_postman_items(item["item"], out)
            continue
        req = item.get("request")
        if not isinstance(req, dict):
            continue
        name = item.get("name") or ""
        method = (req.get("method") or "GET").upper()
        url = req.get("url")
        if isinstance(url, dict):
            url = url.get("raw", "")
        url = url or ""
        headers = {h.get("key"): h.get("value") for h in (req.get("header") or []) if isinstance(h, dict) and h.get("key")}
        out.append({"name": name, "method": method, "url": url, "headers": headers})


def _walk_openapi_paths(spec: Dict, out: List[Dict]) -> None:
    base = ""
    servers = spec.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        base = servers[0].get("url") or ""
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.upper() not in HTTP_METHODS or not isinstance(op, dict):
                continue
            name = op.get("summary") or op.get("operationId") or f"{method.upper()} {path}"
            out.append({"name": name, "method": method.upper(), "url": base.rstrip("/") + path, "headers": {}})


def detect_endpoints_from_document(raw_text: str) -> List[Dict]:
    """Returns [] when `raw_text` isn't a Postman Collection or OpenAPI/
    Swagger JSON document (the normal single-endpoint cURL/plain-text
    flow is unaffected and never routed through this function's
    output). When it IS a multi-endpoint document, returns one entry
    per detected request: {name, method, url, category_guess} — secret
    values in headers/urls are redacted via the same
    detect_and_redact_secrets() gate used before any LLM call, since
    this list is returned directly to the browser for the admin to
    pick from."""
    text = (raw_text or "").strip()
    if not text or text[0] not in "{[":
        return []
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(doc, dict):
        return []

    raw_entries: List[Dict] = []
    if isinstance(doc.get("item"), list):
        _walk_postman_items(doc["item"], raw_entries)
    elif isinstance(doc.get("paths"), dict):
        _walk_openapi_paths(doc, raw_entries)
    else:
        return []

    out = []
    for e in raw_entries:
        redacted_url, _ = detect_and_redact_secrets(e["url"])
        redacted_headers = {}
        for k, v in (e.get("headers") or {}).items():
            redacted_v, _ = detect_and_redact_secrets(str(v))
            redacted_headers[k] = redacted_v
        out.append({
            "name": e["name"], "method": e["method"], "url": redacted_url,
            "headers": redacted_headers,
            "category_guess": _guess_endpoint_category(e["name"], redacted_url),
        })
    return out


# ── Part 7: Postman Smart Grouping — clusters already-detected endpoints
# (detect_endpoints_from_document's output) by their category_guess
# instead of the caller having to generate one Business Action per
# endpoint blindly. Never merges endpoints across different categories
# into one cluster; each cluster can still be expanded/generated
# individually by the caller (this only groups, it never collapses).
_CLUSTER_GROUP_NAMES = {
    "Customer lookup": "Customer", "Order lookup": "Order", "Filtered order search": "Order",
    "Shipment lookup": "Shipment", "Tracking lookup": "Tracking",
    "Filtered shipment search": "Shipment", "Product image": "Product", "LINE OA notification": "Notification",
}


def cluster_endpoints(endpoints: List[Dict]) -> List[Dict]:
    """Returns [{"group": "Customer", "endpoints": [...]}...], preserving
    input order of first appearance per group. "Uncategorized" endpoints
    each stay in their own single-endpoint group rather than being
    lumped together (never merges unrelated endpoints)."""
    groups: Dict[str, List[Dict]] = {}
    order: List[str] = []
    for i, ep in enumerate(endpoints or []):
        category = ep.get("category_guess", "Uncategorized")
        group_name = _CLUSTER_GROUP_NAMES.get(category, category if category != "Uncategorized" else f"Uncategorized #{i+1}")
        if group_name not in groups:
            groups[group_name] = []
            order.append(group_name)
        groups[group_name].append(ep)
    return [{"group": g, "endpoints": groups[g]} for g in order]


def expand_example_questions(seed_question: str, business_purpose: str = "", *,
                              model: str = "gpt-4o-mini", max_tokens: int = 400) -> List[str]:
    """Question Enrichment — expands ONE example customer question into
    several natural Thai phrasings the admin can pick from as chips.
    Always includes the original seed question. Never raises; on any
    AI/parsing failure, falls back to returning just the seed question
    so the caller's UI degrades gracefully instead of erroring."""
    seed_question = (seed_question or "").strip()
    if not seed_question:
        return []
    messages = [
        {"role": "system", "content": (
            "คุณคือผู้ช่วยขยายตัวอย่างคำถามลูกค้าสำหรับแพลตฟอร์ม AI Customer Service "
            "จากคำถามตัวอย่างเดียว ให้เสนอคำถามที่คล้ายกันในความหมายแต่หลากหลายสำนวน "
            "ตอบเป็น JSON array ของ string เท่านั้น ห้ามมีข้อความอื่น ห้ามใช้ ```"
        )},
        {"role": "user", "content": (
            f"ใช้สำหรับ: {business_purpose}\nคำถามตัวอย่าง: {seed_question}\n"
            "โปรดเสนอคำถามที่คล้ายกัน 4-6 ข้อ (ไม่รวมคำถามต้นฉบับ) เป็น JSON array ของ string"
        )},
    ]
    try:
        llm = get_llm_service()
        response = llm.generate(messages, model=model, temperature=0.4, max_tokens=max_tokens)
        suggestions = _parse_json_response(response.text)
        if not isinstance(suggestions, list):
            return [seed_question]
        cleaned = [s.strip() for s in suggestions if isinstance(s, str) and s.strip()]
        result = [seed_question] + [s for s in cleaned if s != seed_question]
        seen, deduped = set(), []
        for q in result:
            if q not in seen:
                seen.add(q)
                deduped.append(q)
        return deduped[:10]
    except Exception:
        return [seed_question]


def generate_suggested_questions(display_name: str, description: str = "", category: str = "",
                                  existing_questions: Optional[List[str]] = None, *,
                                  model: str = "gpt-4o-mini", max_tokens: int = 700) -> List[str]:
    """AI Suggested Questions (bring-back feature) — generates 10-20
    realistic, natural-language Thai customer questions that should
    trigger THIS ERP capability, grouped by similar meaning and deduped,
    mixing short and long phrasings plus common synonyms. Never raises;
    falls back to `existing_questions` (or []) on any AI/parsing failure
    so the caller's UI degrades gracefully instead of erroring."""
    existing_questions = existing_questions or []
    messages = [
        {"role": "system", "content": (
            "คุณคือผู้ช่วยสร้างตัวอย่างคำถามลูกค้าสำหรับแพลตฟอร์ม AI Customer Service "
            "จากชื่อความสามารถ (Capability) หนึ่งอย่าง ให้เสนอคำถามภาษาไทยธรรมชาติที่ลูกค้าจริงน่าจะพิมพ์เข้ามา "
            "เพื่อกระตุ้นให้ AI เรียกใช้ความสามารถนี้ ผสมทั้งคำถามสั้นและยาว รวมคำพ้องความหมาย/คำที่ใช้แทนกันได้ "
            "ห้ามซ้ำความหมายเดิม ตอบเป็น JSON array ของ string เท่านั้น ห้ามมีข้อความอื่น ห้ามใช้ ```"
        )},
        {"role": "user", "content": (
            f"ความสามารถ (Capability): {display_name}\nคำอธิบาย: {description}\nหมวดหมู่: {category}\n"
            "โปรดเสนอคำถามลูกค้าที่เป็นธรรมชาติ 10-20 ข้อ ไม่ซ้ำความหมายกัน เป็น JSON array ของ string"
        )},
    ]
    try:
        llm = get_llm_service()
        response = llm.generate(messages, model=model, temperature=0.5, max_tokens=max_tokens)
        suggestions = _parse_json_response(response.text)
        if not isinstance(suggestions, list):
            return existing_questions
        cleaned = [s.strip() for s in suggestions if isinstance(s, str) and s.strip()]
        seen, deduped = set(), []
        for q in existing_questions + cleaned:
            if q not in seen:
                seen.add(q)
                deduped.append(q)
        return deduped[:20]
    except Exception:
        return existing_questions
