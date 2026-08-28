"""Generic Action Executor — Platform Execution Layer.

Given a Business Action (from services/business_action_registry.py) and an
Execution Context, runs the action and returns a structured Execution
Result. This module does NOT decide which action to run (that is a future
Decision Engine's job) — it only executes the action it is handed.

Architecture (per the platform-first principle in CLAUDE.md):

    Decision Engine (future) -> Business Action Registry -> Action Executor
    -> Execution Adapter (REST / RAG / Tool / Notification / Human /
       Webhook / Workflow) -> Execution Result

Nothing here is ERP- or endpoint-specific: every REST call is built purely
from the Business Action's own `execution`/`parameters` configuration, and
every adapter is looked up generically by `action_type`.
"""
import re
import time
from typing import Any, Callable, Dict, List, Optional

import requests

from services.business_action_registry import (
    get_registry,
    mask_execution_secrets,
    sanitize_for_preview,
    sanitize_response_body,
)

# ── Execution Result ─────────────────────────────────────────────────────
# Every executor returns this same shape — status/result/metadata/latency_ms
# /error/logs — regardless of adapter type, so callers never special-case
# one action type over another.

def _result(status: str, *, result: Optional[Dict] = None, metadata: Optional[Dict] = None,
            error: Optional[str] = None, logs: Optional[List[str]] = None, latency_ms: float = 0.0) -> Dict:
    return {
        "status": status,
        "result": result if result is not None else {},
        "metadata": metadata or {},
        "latency_ms": round(latency_ms, 2),
        "error": error,
        "logs": logs or [],
    }


def _mask_text(text: str, secret_values: Dict[str, Optional[str]]) -> str:
    for value in secret_values.values():
        if value:
            text = text.replace(value, "[MASKED]")
    return text


# ── Execution Context helpers ────────────────────────────────────────────
# Context is a plain dict; every field is optional. Recognized keys:
#   conversation_context, customer_context, collected_slots, workflow,
#   intent, developer_mode, current_user, question, system_values

def _resolve_param_value(param: Dict, context: Dict, secret_values: Dict[str, Optional[str]],
                          grouped_names: Optional[set] = None) -> Optional[str]:
    source = param.get("input_source", "customer_message")
    name = param["name"]
    if source in ("secret_configuration", "credential_store"):
        return secret_values.get(name)
    if source == "customer_message":
        # Explicit customer value takes precedence; a non-required,
        # UNGROUPED customer_message parameter (e.g. Latest) may still
        # carry a configured example_value as its safe default for the
        # turns where the customer didn't say anything — this does not
        # apply to fixed_configuration params, which are never
        # customer-set.
        #
        # Final Conversational Correctness (2026-08-15): a parameter that
        # is a member of a parameter GROUP (AT_LEAST_ONE/EXACTLY_ONE/ALL,
        # e.g. GetDataCustomer's CustCode/CustEmail/CustName/CustPhone)
        # must NEVER fall back to example_value here — example_value
        # holds documentation/Test-Action placeholder data (e.g.
        # "customer@example.com"), never a real identifier. Confirmed
        # live: a customer message that only supplied CustCode was still
        # sending fabricated CustEmail/CustName/CustPhone example values
        # to the real ERP alongside it. The Decision Engine's own
        # validate_can_execute() already confirms the group is satisfied
        # by a REAL collected value before execution is ever reached, so
        # an unfilled sibling group member must simply stay absent from
        # the outgoing request, not be silently fabricated.
        value = (context.get("collected_slots") or {}).get(name)
        if value is not None:
            return value
        if name in (grouped_names or set()):
            return None
        return param.get("example_value") if not param.get("required") else None
    if source == "customer_profile":
        return (context.get("customer_context") or {}).get(name)
    if source == "conversation_context":
        return (context.get("conversation_context") or {}).get(name)
    if source == "fixed_configuration":
        return param.get("fixed_value") or param.get("example_value")
    if source == "system_generated":
        return (context.get("system_values") or {}).get(name)
    return None


def _collect_parameter_values(action: Dict, context: Dict, secret_values: Dict[str, Optional[str]]) -> Dict[str, str]:
    grouped_names = {m for g in (action.get("parameter_groups") or []) for m in (g.get("members") or [])}
    values = {}
    for param in action.get("parameters") or []:
        value = _resolve_param_value(param, context, secret_values, grouped_names)
        if value not in (None, ""):
            values[param["name"]] = value
    return values


# ── REST Executor ────────────────────────────────────────────────────────

def _build_rest_headers(execution: Dict) -> Dict[str, str]:
    headers = dict(execution.get("headers") or {})
    auth_type = execution.get("auth_type")
    auth_config = execution.get("auth_config") or {}
    if auth_type == "bearer" and auth_config.get("bearer_token"):
        headers["Authorization"] = f"Bearer {auth_config['bearer_token']}"
    elif auth_type == "api_key" and auth_config.get("api_key"):
        headers[auth_config.get("header_name", "X-Api-Key")] = auth_config["api_key"]
    elif auth_type == "custom_header":
        headers.update(auth_config.get("headers") or {})
    return headers


# Tracking Record Selection fix (P0 Final Blocker Closure, 2026-08-28) —
# confirmed live: SearchDataShipmentList's own `response_mapping` always
# reads `$.data.0.X` ("the latest shipment"), and this action has no
# "Tracking" parameter configured at all, so a customer-provided China
# tracking number (e.g. "เช็ก Tracking 79017107089341") was never used to
# select a record — the API's own "Latest 5" response for the customer's
# CustCode was returned as-is, silently substituting whichever shipment
# happened to be newest, even when that shipment's own TrackingCH/
# TrackingTH plainly did NOT match the number the customer asked about.
# A purely GENERIC, structural fix (works for ANY action whose response
# has this shape, never hardcoded to one action_key): if the raw response
# has a top-level "data" list of dicts that carry Tracking-shaped fields,
# and the customer's own message names an identifier-shaped token, search
# every item for an exact match on TrackingCH/TrackingTH and move it to
# index 0 (so the EXISTING "$.data.0.X" response_mapping convention picks
# it up completely unchanged); if the message names such a token but NO
# item matches, clear the list to empty (-> $.data.0.X maps to None ->
# "not found" downstream, never a silent wrong-record substitution). A
# complete no-op whenever the response has no Tracking-shaped field at
# all, or the message names no identifier-shaped token — e.g. "FT3182 มี
# Order อะไรบ้าง" / "เช็ก Shipment ของผมให้หน่อย" (no specific value named)
# still correctly default to the latest record, unaffected. Confirmed
# live: a bare CustCode mentioned in the message (e.g. "FT3182 มี Order
# อะไรบ้าง") is ITSELF identifier-shaped, so it must be excluded from the
# candidate tokens — it already IS the authenticated request parameter,
# never "a specific Tracking value being searched for".
_TRACKING_FIELD_NAMES = ("TrackingCH", "TrackingTH")
_IDENTIFIER_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_/]{3,19}$")


def _reorder_data_by_tracking_match(response_body: Any, customer_message: Optional[str],
                                     request_values: Optional[Dict[str, str]] = None) -> None:
    if not customer_message or not isinstance(response_body, dict):
        return
    data = response_body.get("data")
    if not isinstance(data, list) or not data or not all(isinstance(d, dict) for d in data):
        return
    tracking_fields = [f for f in _TRACKING_FIELD_NAMES if any(f in d for d in data)]
    if not tracking_fields:
        return
    already_used_values = {str(v) for v in (request_values or {}).values() if v}
    tokens = [t for t in re.split(r"[\s,;]+", customer_message) if t]
    identifier_tokens = [t for t in tokens if _IDENTIFIER_TOKEN_RE.match(t) and any(ch.isdigit() for ch in t)
                         and t not in already_used_values]
    if not identifier_tokens:
        return
    for i, d in enumerate(data):
        for field in tracking_fields:
            val = d.get(field)
            if val and any(tok == val for tok in identifier_tokens):
                if i != 0:
                    data.insert(0, data.pop(i))
                return
    # An identifier-shaped token was named but matched no returned record —
    # never fall back to whatever the API happened to return at index 0.
    data.clear()


def run_rest_call(execution: Dict, request_values: Dict[str, str], headers: Dict[str, str],
                   response_mapping: Optional[List[Dict]] = None,
                   secret_values: Optional[Dict[str, Optional[str]]] = None,
                   customer_message: Optional[str] = None) -> Dict:
    """Shared low-level REST call — used by both the REST Executor and the
    Business Action Center's manual "Test Action" (admin/routes.py), so the
    two never drift apart. Substitutes {path_param} placeholders in the
    endpoint, sends the remainder as query/body per method+content_type,
    retries transient failures per execution.retry_policy, and always
    returns a JSON-serializable, secret-masked result."""
    secret_values = secret_values or {}
    endpoint = execution.get("endpoint") or ((execution.get("base_url") or "").rstrip("/") + (execution.get("endpoint_path") or ""))
    method = (execution.get("http_method") or "GET").upper()
    content_type = execution.get("content_type", "application/json")
    timeout = execution.get("timeout_seconds") or 10
    retry_policy = execution.get("retry_policy") or {}
    max_retries = max(0, int(retry_policy.get("max_retries") or 0))
    backoff_seconds = float(retry_policy.get("backoff_seconds") or 0.5)

    remaining_values = dict(request_values)
    path_params = {}
    for key in list(remaining_values.keys()):
        placeholder = "{" + key + "}"
        if placeholder in endpoint:
            endpoint = endpoint.replace(placeholder, str(remaining_values.pop(key)))
            path_params[key] = "[substituted]"

    preview_body = {k: ("[MASKED]" if k in secret_values else sanitize_for_preview(v)) for k, v in remaining_values.items()}
    request_preview = {
        "endpoint": endpoint, "method": method, "content_type": content_type,
        "headers": mask_execution_secrets({"headers": headers})["headers"],
        "body": preview_body, "path_params": path_params,
    }

    attempt = 0
    last_exc = None
    t0 = time.time()
    while attempt <= max_retries:
        try:
            kwargs = {"headers": headers, "timeout": timeout}
            if method in ("POST", "PUT", "PATCH"):
                if content_type == "application/x-www-form-urlencoded":
                    kwargs["data"] = remaining_values
                elif content_type == "multipart/form-data":
                    kwargs["files"] = {k: (None, str(v)) for k, v in remaining_values.items()}
                else:
                    kwargs["json"] = remaining_values
            else:
                kwargs["params"] = remaining_values
            resp = requests.request(method, endpoint, **kwargs)
            elapsed_ms = round((time.time() - t0) * 1000, 1)
            try:
                response_body = resp.json()
            except Exception:
                response_body = resp.text
            _reorder_data_by_tracking_match(response_body, customer_message, request_values)
            sanitized_response = sanitize_response_body(response_body)
            detected_keys = list(response_body.keys()) if isinstance(response_body, dict) else []
            mapped = {}
            if isinstance(response_body, dict):
                for m in response_mapping or []:
                    path = m["json_path"].lstrip("$.")
                    value = response_body
                    for part in path.split("."):
                        if isinstance(value, dict):
                            value = value.get(part)
                        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                            value = value[int(part)]
                        else:
                            value = None
                    mapped[m["mapped_label"]] = sanitize_for_preview(value) if isinstance(value, str) else value

            friendly_error = None
            if resp.status_code == 401:
                friendly_error = "API ตอบกลับด้วยสถานะ 401 (ไม่ผ่านการยืนยันตัวตน)"
            elif resp.status_code == 404:
                friendly_error = "ไม่พบข้อมูลที่ค้นหา"
            elif resp.status_code >= 500:
                friendly_error = f"API ตอบกลับด้วยสถานะ {resp.status_code} (ฝั่งเซิร์ฟเวอร์ปลายทางขัดข้อง)"
            elif resp.status_code >= 400:
                friendly_error = f"API ตอบกลับด้วยสถานะ {resp.status_code}"

            if friendly_error and resp.status_code >= 500 and attempt < max_retries:
                attempt += 1
                time.sleep(backoff_seconds * attempt)
                continue

            return {
                "request": request_preview, "status_code": resp.status_code, "response": sanitized_response,
                "detected_top_level_keys": detected_keys, "mapped_fields": mapped,
                "execution_time_ms": elapsed_ms, "error": friendly_error, "attempts": attempt + 1,
            }
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < max_retries:
                attempt += 1
                time.sleep(backoff_seconds * attempt)
                continue
            break

    elapsed_ms = round((time.time() - t0) * 1000, 1)
    error_text = _mask_text(str(last_exc), secret_values)
    is_timeout = isinstance(last_exc, requests.exceptions.Timeout) or "timeout" in error_text.lower()
    friendly = "การเชื่อมต่อหมดเวลา (Timeout)" if is_timeout else "เชื่อมต่อ API ไม่สำเร็จ"
    return {
        "request": request_preview, "status_code": None, "response": None,
        "detected_top_level_keys": [], "mapped_fields": {}, "execution_time_ms": elapsed_ms,
        "error": friendly, "attempts": attempt + 1,
    }


_CREDENTIAL_ERROR_MESSAGES = {
    "credential_not_found": "ไม่พบข้อมูลเชื่อมต่อที่บันทึกไว้",
    "credential_disabled": "ข้อมูลเชื่อมต่อถูกปิดใช้งานอยู่",
    "credential_revoked": "ข้อมูลเชื่อมต่อถูกเพิกถอนแล้ว",
    "encryption_key_unavailable": "ระบบเข้ารหัสยังไม่พร้อมใช้งาน (ไม่พบ Master Key)",
    "encryption_key_invalid": "ไม่สามารถถอดรหัสข้อมูลเชื่อมต่อได้ด้วยกุญแจปัจจุบัน",
    "environment_variable_not_set": "ยังไม่ได้ตั้งค่า environment variable ที่เกี่ยวข้อง",
}


def _friendly_credential_error(registry, action_id: str, missing_names: List[str]) -> str:
    """Structured, per-parameter credential/secret error — never the
    raw exception, never a value. Falls back to the legacy generic
    message if the registry can't explain WHY (e.g. an older registry
    build without resolve_secret_parameter_errors)."""
    try:
        per_param_errors = registry.resolve_secret_parameter_errors(action_id)
    except AttributeError:
        per_param_errors = {}
    parts = []
    for name in missing_names:
        reason = per_param_errors.get(name, "environment_variable_not_set")
        parts.append(f"{name}: {_CREDENTIAL_ERROR_MESSAGES.get(reason, reason)}")
    return "ไม่พบข้อมูลเชื่อมต่อที่จำเป็น — " + " / ".join(parts)


def _execute_rest(action: Dict, context: Dict, registry) -> Dict:
    execution = action.get("execution")
    if not execution or not (execution.get("endpoint") or execution.get("base_url")):
        return _result("error", error="ยังไม่ได้ตั้งค่า Endpoint สำหรับ Action นี้")

    secret_values = registry.resolve_secret_parameters(action["id"])
    missing_secrets = [name for name, value in secret_values.items() if value is None]
    if missing_secrets:
        return _result("error", error=_friendly_credential_error(registry, action["id"], missing_secrets))

    provided = _collect_parameter_values(action, context, secret_values)
    # Secret-sourced parameters are resolved server-side (never supplied by
    # the caller), but validate_can_execute() still needs to see them
    # present to correctly evaluate "required" — only the *value* stays
    # server-side; the caller-facing `provided` dict passed to REST later
    # already includes them via `provided` itself.
    validation = registry.validate_can_execute(action["id"], provided)
    if not validation["ok"]:
        parts = []
        if validation["missing_required"]:
            parts.append("ขาดข้อมูลที่จำเป็น: " + ", ".join(validation["missing_required"]))
        for g in validation["failed_groups"]:
            parts.append(f"ต้องมีอย่างน้อยหนึ่งค่าจาก: {', '.join(g['members'])}" if g["rule"] == "AT_LEAST_ONE"
                          else f"เงื่อนไขกลุ่ม '{g['name']}' ({g['rule']}) ไม่ผ่าน")
        return _result("error", error=" / ".join(parts) or "ข้อมูลไม่ครบตามเงื่อนไข")

    headers = _build_rest_headers(execution)
    outcome = run_rest_call(execution, provided, headers, action.get("response_mapping"), secret_values,
                             customer_message=context.get("question"))
    status = "error" if outcome.get("error") else "success"
    return _result(status, result=outcome, error=outcome.get("error"), latency_ms=outcome.get("execution_time_ms") or 0.0)


# ── RAG Executor ──────────────────────────────────────────────────────────

def _execute_rag(action: Dict, context: Dict, registry) -> Dict:
    question = context.get("question") or (context.get("conversation_context") or {}).get("last_message")
    if not question:
        return _result("error", error="ไม่มีคำถามสำหรับค้นหา (question is required in context)")
    t0 = time.time()
    try:
        from services.rag_service import get_rag_service
        rag = get_rag_service()
        chunks = rag.retrieve(question, top_k=3)
        answer = rag.build_context(chunks) if chunks else ""
        citations = [
            {"filename": c.get("filename"), "url": c.get("public_url") or c.get("download_url"), "score": c.get("score")}
            for c in chunks if c.get("filename")
        ]
        confidence = max((c.get("score") or 0.0) for c in chunks) if chunks else 0.0
        latency_ms = (time.time() - t0) * 1000
        return _result("success", result={"answer": answer, "citations": citations, "confidence": confidence,
                                           "chunk_count": len(chunks)}, latency_ms=latency_ms)
    except Exception as e:
        return _result("error", error=f"RAG retrieval failed: {sanitize_for_preview(str(e))}", latency_ms=(time.time() - t0) * 1000)


# ── Tool Executor ─────────────────────────────────────────────────────────
# Internal, deterministic Python tools — no network calls except where the
# tool itself is inherently network-based (none of the current tools are).

def _tool_calculator(context: Dict) -> Dict:
    question = context.get("question") or ""
    from rag.calculator import answer_calculation_question
    outcome = answer_calculation_question(question)
    if not outcome:
        return {"ok": False, "reason": "no_matching_calculation"}
    return outcome


def _tool_url_converter(context: Dict) -> Dict:
    """Normalizes a URL and converts known share-link formats (currently:
    Google Drive "view" links) into a direct-access URL. Deterministic,
    no network call — a template for future tools (e.g. OCR)."""
    url = (context.get("system_values") or {}).get("url") or context.get("url") or ""
    if not url:
        return {"ok": False, "reason": "no_url_provided"}
    import re
    m = re.search(r"drive\.google\.com/file/d/([\w-]+)", url)
    if m:
        return {"ok": True, "original_url": url, "converted_url": f"https://drive.google.com/uc?export=download&id={m.group(1)}",
                "conversion": "google_drive_direct_download"}
    return {"ok": True, "original_url": url, "converted_url": url, "conversion": "none_needed"}


# Business Action Provider lookups (Business Action Framework +
# ERP Sync milestone, Phase 2/6) — dispatches to
# services/business_action_providers.py::get_provider(category), which
# is the one seam that decides mock vs. a future real ERP connector.
# `context["action_params"]` (a manual Playground test payload) is tried
# first, falling back to `collected_slots` (the existing conversational
# slot-filling shape) so this works from either caller unchanged.
def _tool_business_provider_lookup(category: str, context: Dict) -> Dict:
    from services.business_action_providers import get_provider
    params = dict(context.get("collected_slots") or {})
    params.update(context.get("action_params") or {})
    return get_provider(category).lookup(params)


def _tool_customer_lookup(context: Dict) -> Dict:
    return _tool_business_provider_lookup("customer", context)


def _tool_order_lookup(context: Dict) -> Dict:
    return _tool_business_provider_lookup("order", context)


def _tool_tracking_lookup(context: Dict) -> Dict:
    return _tool_business_provider_lookup("tracking", context)


def _tool_finance_lookup(context: Dict) -> Dict:
    return _tool_business_provider_lookup("finance", context)


def _tool_product_lookup(context: Dict) -> Dict:
    return _tool_business_provider_lookup("product", context)


TOOL_REGISTRY: Dict[str, Callable[[Dict], Dict]] = {
    "calculator": _tool_calculator,
    "url_converter": _tool_url_converter,
    "customer_lookup": _tool_customer_lookup,
    "order_lookup": _tool_order_lookup,
    "tracking_lookup": _tool_tracking_lookup,
    "finance_lookup": _tool_finance_lookup,
    "product_lookup": _tool_product_lookup,
}


def _execute_tool(action: Dict, context: Dict, registry) -> Dict:
    tool_name = (action.get("execution") or {}).get("execution_target") or action.get("action_key")
    tool_fn = TOOL_REGISTRY.get(tool_name)
    if not tool_fn:
        return _result("error", error=f"Unknown internal tool: {tool_name}",
                        logs=[f"Registered tools: {', '.join(TOOL_REGISTRY)}"])
    t0 = time.time()
    try:
        outcome = tool_fn(context)
        latency_ms = (time.time() - t0) * 1000
        status = "success" if outcome.get("ok", True) else "error"
        return _result(status, result=outcome, error=None if status == "success" else outcome.get("reason"), latency_ms=latency_ms)
    except Exception as e:
        return _result("error", error=f"Tool execution failed: {sanitize_for_preview(str(e))}", latency_ms=(time.time() - t0) * 1000)


# ── Notification Executor (interface only — no channel implemented) ──────

def _execute_notification(action: Dict, context: Dict, registry) -> Dict:
    payload = {
        "channel": (action.get("execution") or {}).get("execution_target") or "unspecified",
        "message": context.get("question") or "",
        "recipient_hint": (context.get("customer_context") or {}).get("customer_code"),
    }
    return _result("not_implemented", result={"payload": payload},
                    logs=["Notification Executor is an interface-only placeholder — no channel (e.g. LINE) is wired up yet."])


# ── Human Handoff Executor (interface only — no one is notified) ─────────

def _execute_human_handoff(action: Dict, context: Dict, registry) -> Dict:
    handoff_payload = {
        "reason": context.get("intent") or "customer_request",
        "workflow": context.get("workflow"),
        "collected_slots": context.get("collected_slots") or {},
        "conversation_context": context.get("conversation_context") or {},
        "current_user": context.get("current_user"),
    }
    return _result("handoff_prepared", result={"handoff_payload": handoff_payload},
                    logs=["Human Handoff Executor prepared a payload only — no human has been notified."])


# ── Webhook Executor (generic outgoing webhook) ───────────────────────────

def _execute_webhook(action: Dict, context: Dict, registry) -> Dict:
    execution = action.get("execution")
    if not execution or not (execution.get("endpoint") or execution.get("base_url")):
        return _result("error", error="ยังไม่ได้ตั้งค่า Webhook Endpoint สำหรับ Action นี้")
    secret_values = registry.resolve_secret_parameters(action["id"])
    provided = _collect_parameter_values(action, context, secret_values)
    headers = _build_rest_headers(execution)
    execution = {**execution, "http_method": execution.get("http_method") or "POST"}
    outcome = run_rest_call(execution, provided, headers, action.get("response_mapping"), secret_values)
    status = "error" if outcome.get("error") else "success"
    return _result(status, result=outcome, error=outcome.get("error"), latency_ms=outcome.get("execution_time_ms") or 0.0)


# ── Workflow Executor (placeholder — future support) ──────────────────────

def _execute_workflow(action: Dict, context: Dict, registry) -> Dict:
    return _result("not_implemented", result={"workflow": (action.get("execution") or {}).get("execution_target")},
                    logs=["Workflow Executor is a placeholder — multi-step workflow orchestration is future work."])


# ── Dispatcher ─────────────────────────────────────────────────────────────

_EXECUTORS: Dict[str, Callable[[Dict, Dict, Any], Dict]] = {
    "API": _execute_rest,
    "RAG": _execute_rag,
    "TOOL": _execute_tool,
    "NOTIFICATION": _execute_notification,
    "HUMAN_HANDOFF": _execute_human_handoff,
    "WEBHOOK": _execute_webhook,
    "WORKFLOW": _execute_workflow,
}


class ActionExecutor:
    """The one, common Execution entry point: `execute(action_id, context)`.
    Looks up the action from the Business Action Registry, dispatches to
    the adapter matching `action_type`, and always returns a structured
    Execution Result — it never raises, so a future Decision Engine can
    call this without a try/except of its own."""

    def __init__(self, sb=None):
        self.registry = get_registry(sb) if sb is not None else get_registry()

    def execute(self, action_id: str, context: Optional[Dict] = None) -> Dict:
        context = context or {}
        start = time.time()
        action = self.registry.get_full(action_id, mask_secrets=False)
        if not action:
            return _result("error", error="Action not found", metadata={"action_id": action_id},
                            latency_ms=(time.time() - start) * 1000)
        if not action.get("enabled"):
            return _result("error", error="Action is disabled",
                            metadata={"action_id": action_id, "action_key": action.get("action_key")},
                            latency_ms=(time.time() - start) * 1000)

        # Authorization Gate (Task 06, 2026-08-26) — checked here, the ONE
        # choke point every caller (LINE webhook, Admin Playground, an
        # admin route, or a future direct service call) must go through
        # to actually run a Business Action, so this can never be
        # bypassed by routing around the conversational layer. See
        # services/authorization_service.py's module docstring for the
        # confirmed root cause (no verified LINE-user-to-customer-account
        # binding exists anywhere) and why this fails closed rather than
        # trusting a customer-supplied CustCode/OrderCode/ShipmentCode as
        # proof of ownership.
        from services.authorization_service import check_authorization, AUTHORIZATION_DENIED_MESSAGE
        auth = check_authorization(action, context, sb=self.registry._sb)
        if not auth["authorized"]:
            return _result(
                "denied",
                result={"message": AUTHORIZATION_DENIED_MESSAGE},
                error=None,
                metadata={"action_id": action_id, "action_key": action.get("action_key"),
                          "authorization_denied_reason": auth["reason"]},
                latency_ms=(time.time() - start) * 1000,
            )

        action_type = action.get("action_type")
        executor_fn = _EXECUTORS.get(action_type)
        if not executor_fn:
            return _result("error", error=f"Unsupported action_type: {action_type}",
                            metadata={"action_id": action_id}, latency_ms=(time.time() - start) * 1000)

        try:
            result = executor_fn(action, context, self.registry)
        except Exception as e:
            result = _result("error", error=f"Executor crashed: {sanitize_for_preview(str(e))}")

        result.setdefault("metadata", {})
        result["metadata"].update({
            "executor": action_type, "action_id": action_id, "action_key": action.get("action_key"),
            "action_name": action.get("display_name") or action.get("name"),
        })
        if context.get("developer_mode"):
            result["metadata"]["developer_mode"] = {
                "input_parameters": {k: v for k, v in (context.get("collected_slots") or {}).items()},
                "output_summary": _summarize_result(result.get("result")),
                "status": result["status"],
            }
        result["latency_ms"] = round((time.time() - start) * 1000, 2)
        return result


def _summarize_result(result: Any) -> str:
    if isinstance(result, dict):
        keys = list(result.keys())[:6]
        return f"dict with keys: {', '.join(keys)}"
    if isinstance(result, list):
        return f"list of {len(result)} item(s)"
    return sanitize_for_preview(str(result))[:200]


def get_action_executor(sb=None) -> ActionExecutor:
    return ActionExecutor(sb)
