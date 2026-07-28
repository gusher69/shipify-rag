# Business Action Center

Platform-first registry of configurable **Business Actions** — the abstraction layer that replaces hardcoded integrations ("Shipify Tracking API", "China Warehouse Lookup") with a generic, config-driven concept any current or future customer/industry can reuse: **Status Inquiry Workflow**, **Inventory Location Workflow**, **Case Management Workflow**, etc.

This is a **registry**, executed by the Generic Action Executor (`services/action_executor.py`) and orchestrated by the AI Middleware Decision Engine (`services/decision_engine.py`, see [DECISION_ENGINE.md](DECISION_ENGINE.md)) — neither ever hardcodes a specific API endpoint, ERP vendor, or workflow name; both read exclusively from this registry.

## Business Action Center (Admin UI)

Route: `/admin/ai/business-actions` (template: `admin/templates/business_actions.html`).

Features: List, Create, Edit, Delete (soft), Enable/Disable, Duplicate, Search, Filter by Action Type, Import/Export JSON, Test Action (runs independently of the AI). The Action Editor has 9 tabs: General, AI Metadata, Parameters, Execution, Response Mapping, Validation, Examples, Debug, Test.

**Default creation flow is now AI-Assisted API Auto Setup** (paste API docs → AI proposes config → review/approve/test) — see [AI_AUTO_SETUP.md](AI_AUTO_SETUP.md). The Manual Wizard and this 9-tab Advanced Editor remain fully available, unchanged.

## Registry Service

`services/business_action_registry.py` — `BusinessActionRegistry` class + `get_registry(sb)` factory (same pattern as `services/rag_service.py::get_rag_service()`).

Decision-Engine-facing surface (read-only w.r.t. execution):
```python
registry.get(action_id)
registry.get_by_key(action_key)
registry.list()
registry.search(query)          # deterministic substring search — NOT semantic yet
registry.enabled_actions()
registry.find_by_category(category)
registry.find_by_type(action_type)
registry.get_full(action_id)    # action + examples + parameters + execution (masked) + mapping + validation + tags
```

Admin-UI-facing CRUD surface: `create`, `update`, `delete`, `set_enabled`, `duplicate`, `upsert_execution`, `replace_examples/parameters/response_mapping/validation_rules/tags`, `prepare_embedding_source`, `export_action`, `import_action`.

## Database

Migration: `migrations/026_business_action_center.sql`. Normalized, matches this project's existing conventions (UUID PKs, RLS + service_role policy, soft delete via `deleted_at` like `knowledge_items`).

| Table | Purpose |
|---|---|
| `business_actions` | Core record — General/Classification/AI Metadata/Workflow-level rules/Response prompts/Developer fields |
| `business_action_examples` | Example Questions / Trigger Examples |
| `business_action_parameters` | Required/Optional Parameters, validation_type, future `slot_type` (Information Collection Engine binding) |
| `business_action_execution` | Endpoint, HTTP method, headers, auth (secrets masked on every read), timeout, retry policy |
| `business_action_response_mapping` | JSON-path → human label mapping (e.g. `$.status` → "Shipment Status") |
| `business_action_validation` | regex / length rules today; checksum / external prepared as future types |
| `business_action_tags` | Free-form tags for search/filter |
| `business_action_embeddings` | `embedding_source_text` + `VECTOR(3072)` column (same convention as `knowledge_chunks`, migration 019) — **column stays NULL**; architecture only, not computed or used for routing in this task |

## Action Types

`RAG | API | TOOL | WORKFLOW | NOTIFICATION | HUMAN_HANDOFF | WEBHOOK` — adding a new type is a one-line change to `ACTION_TYPES` in the registry service; no other module needs to change.

## Execution Model

`business_action_execution` holds everything needed to call an external system: `endpoint`, `http_method` (GET/POST/PUT/PATCH/DELETE), `headers`, `auth_type` (none/bearer/api_key/custom_header/oauth-future), `auth_config` (secret values), `timeout_seconds`, `retry_policy`. **No ERP vendor is ever hardcoded** — `execution_target` is a free-form label only.

`Test Action` (`POST /admin/api/business-actions/{id}/test`) executes this configuration directly via `requests`, independent of the AI pipeline — returns request (with secrets masked), response, mapped fields (via `response_mapping`), execution time, and errors.

**Security**: every API response and UI render passes execution data through `mask_execution_secrets()` — any header/auth_config key matching `token|secret|key|password|authorization|api_key|bearer` is masked to `****last4chars`. Raw secrets are only ever used server-side inside `api_test_business_action`, never returned to the client.

## Embedding Preparation

`build_embedding_source_text()` concatenates Action Name + Description + AI Description + Business Description + Example Questions + Tags + Search Keywords into one text blob. `registry.prepare_embedding_source(action_id)` persists this text into `business_action_embeddings` with `status="pending"`. **No embedding provider is called and no vector is computed** — this task only prepares the architecture a future semantic-routing task will populate.

## Decision Engine Integration (now implemented — see DECISION_ENGINE.md)

The AI Middleware Decision Engine's contract with this registry, as actually implemented in `services/decision_engine.py::search_candidate_actions()` / `select_best_action()`:
1. Resolve intent/workflow (existing classifiers only) → call `registry.enabled_actions()`, score candidates by category/keyword/example/AI-description overlap/parameter availability/priority (semantic + embedding scores are prepared interfaces, always 0.0 today).
2. If the selected action needs parameters, defer to the existing Information Collection Engine (`services/slot_filling_engine.py`) — the Decision Engine never re-implements slot filling.
3. Execute via the Generic Action Executor (`services/action_executor.py`), which reads `business_action_execution` server-side only.
4. Read `execution result.mapped_fields` (from `business_action_response_mapping`) to build the customer-facing reply.

See [DECISION_ENGINE.md](DECISION_ENGINE.md) for the full architecture, routing order, and future phases (semantic routing, embeddings, LINE OA, Notification Adapter).

## Simple Mode vs. Advanced Mode

Clicking **+ New Business Action** now opens a **Choose Action Type** picker (Connect an API / Search Knowledge–RAG / Internal Tool / Human Handoff / Notification / Workflow / Webhook / Advanced-Other) instead of going straight to the 9-tab editor.

- **Connect an API** opens the **Simple Mode API Wizard** (`#ba-wizard` in `business_actions.html`) — a 5-step guided flow described below. It writes to the exact same tables/columns as the Advanced editor via the exact same `PUT`/`PATCH` endpoints, so a wizard-created action is 100% editable in Advanced Mode afterward.
- Every other type opens the existing **Advanced Mode** editor (`#ba-modal`, unchanged, all 9 tabs) with `Action Type` preset.
- From any wizard step, **"Advanced Settings →"** saves progress so far and jumps straight into the Advanced editor for that action — nothing is hidden or lost, it's the same record.

Advanced Mode is untouched by this task: same 9 tabs, same fields (Internal Action ID, Version, Priority, Tags, Raw Headers, Retry Policy, Response Mapping, Validation Rules, Debug Notes, Import/Export JSON, full Test Payload editor).

## API Action Wizard (5 steps)

1. **What does this API do?** — Action Name, Display Name, Description, Category, Enabled. Action ID is auto-generated from the Action Name (client-side slug, mirrors `generate_action_key()`) and shown read-only. Version/Priority/Tags are not shown here (available in Advanced Mode).
2. **When should AI use it?** ("AI Usage Rules") — AI Description, Example Customer Questions (one per line), Keywords.
3. **What information is required?** — add Parameter cards (Name, Display Name, Required, Input Source, Example, Description). Input Source options: `customer_message`, `customer_profile`, `conversation_context`, `fixed_configuration`, `secret_configuration`, `system_generated`. Choosing `secret_configuration` reveals a **Secret Reference** field (an environment variable name only). One optional **Parameter Group** field lets an admin type a comma-separated list of parameter names that form a generic `AT_LEAST_ONE` requirement (e.g. "ต้องมีอย่างน้อย 1 ค่า จาก 4 รายการนี้").
4. **Connect API** — Base URL, Endpoint Path (with a live Final URL preview), HTTP Method, Content Type, Timeout, Headers (JSON), Authentication (none / bearer / api key), and a **Test Connection** button that saves the execution config and fires an empty test call to sanity-check reachability.
5. **Test and Save** — pick one non-secret parameter as the test identifier, **Send Test Request** (calls the same `/test` endpoint Advanced Mode uses), review the sanitized response, then **Save and Enable**.

## Parameter Groups (generic, reusable)

`business_actions.parameter_groups` (JSONB, migration `027_business_action_parameter_groups.sql`) holds rules like:
```json
[{"name": "customer_search_identifier", "rule": "AT_LEAST_ONE", "members": ["CustCode", "CustEmail", "CustName", "CustPhone"]}]
```
Supported rules: `ALL`, `AT_LEAST_ONE`, `EXACTLY_ONE`, `OPTIONAL` — evaluated by `validate_parameter_groups()` / `registry.validate_can_execute()`. The mechanism is intentionally action-agnostic: the same code path also validates e.g. a future Order API's "order_number OR tracking_number" group or a Login API's "email OR phone" group — no per-action special-casing.

## Secret References (no vault, minimal abstraction)

`business_action_parameters.secret_ref` stores only an **environment variable name**, never a secret value. `resolve_secret_ref(name)` (`services/business_action_registry.py`) is the single place a real secret value is ever read (`os.environ.get(name)`), and only at execution time inside `api_test_business_action` / the future Decision Engine executor. Parameters with `input_source = "secret_configuration"` are always forced `visible_to_customer=False`, `visible_in_developer_mode=False`, `loggable=False`, and are stripped from any caller-supplied `parameters` payload before validation. This is intentionally the smallest possible secret-reference abstraction — not a secrets vault/marketplace.

## GetDataCustomer (customer_data_lookup) — first real ERP action

Seeded via `python -m tools.seed_business_action_getdatacustomer` (idempotent — reuses the existing action if already present). Configuration:

- Action ID `customer_data_lookup`, Type `API`, Category `customer`, enabled.
- Endpoint: `https://fasttrade.in.th/web-service/ai-chat/GetDataCustomer`, `POST`, `application/x-www-form-urlencoded`, header `Accept: application/json`.
- Parameters: `SecretCode` (secret reference `FASTTRADE_AI_CHAT_SECRET_CODE`, never customer/log/debug visible) plus `CustCode` / `CustEmail` / `CustName` / `CustPhone`, grouped under `customer_search_identifier` (`AT_LEAST_ONE`, minimum 1).
- The real secret value must be set as the `FASTTRADE_AI_CHAT_SECRET_CODE` environment variable outside source control (see `.env.example`) — it is never present in any migration, seed script, or test fixture.
- **Response schema is not hardcoded**: the production JSON shape from FastTrade hasn't been confirmed by a real successful call yet, so no response fields are assumed — `Test Action` will surface whatever top-level keys a real call actually returns.

## Testing an API Action

`POST /admin/api/business-actions/{id}/test` (used by both Advanced Mode's Test tab and the wizard's Step 5):
1. Validates parameter groups (`validate_can_execute`) — returns a Thai message like "ต้องมีอย่างน้อย 1 ค่า จาก 4 รายการนี้" if unsatisfied, without ever calling the API.
2. Resolves required secrets server-side; if a referenced env var isn't set, returns "ไม่พบ Secret Configuration (…)" without attempting the call.
3. Executes via `requests`, sanitizes the response (`sanitize_response_body` masks email/phone-shaped fields), and maps HTTP status to a friendly Thai message (401/404/4xx/5xx/connection errors) — the Admin page never crashes on an external failure.
4. The request preview always masks `SecretCode`-style parameters as the literal `"[MASKED]"` (never partial) — this is stricter than the generic header/auth masking (which keeps the last 4 characters) because this class of parameter must never be inferable from the UI.

## Security and Masking Summary

| Layer | Behavior |
|---|---|
| Database | Never stores a secret value — only an env var name (`secret_ref`) |
| API response (`get`, `get_full`, `export`) | Secret-configuration parameters carry no value field at all |
| Request preview (Test Action) | Secret parameters shown as `[MASKED]`; other free text passed through `sanitize_for_preview()` |
| Response body (Test Action) | PII-shaped fields (email/phone patterns) masked via `sanitize_response_body()` |
| Server logs / exceptions | Any resolved secret value is string-replaced with `[MASKED]` before logging |
| List page | Shows only the endpoint **host**, never the full URL, query string, or auth value |

## How to Add Another ERP API (generic path — not specific to GetDataCustomer)

1. In the Admin UI: **+ New Business Action → Connect an API**.
2. Steps 1–2: name it, describe it, give a few example customer questions.
3. Step 3: add parameters; if the API needs one-of-several identifiers, list them in the Parameter Group field. If it needs a secret, set Input Source to "secret_configuration" and give it a new environment variable name (add that variable to `.env` outside source control).
4. Step 4: enter Base URL + Endpoint Path, method, content type, headers.
5. Step 5: test with a real identifier and Save and Enable.
6. Optional: open Advanced Settings to configure Response Mapping, Validation Rules, retry policy, or Debug Notes.

### วิธีเพิ่ม API ใหม่แบบง่าย (สำหรับผู้ดูแลระบบ)

1. กดปุ่ม **"+ New Business Action"** แล้วเลือก **"🔌 Connect an API"**
2. ขั้นตอนที่ 1–2: ตั้งชื่อ Action, คำอธิบาย และตัวอย่างคำถามที่ลูกค้าจะถาม
3. ขั้นตอนที่ 3: เพิ่มพารามิเตอร์ที่ API ต้องการ — ถ้าต้องมีอย่างน้อยหนึ่งค่าจากหลายรายการ (เช่น รหัสลูกค้า หรือ อีเมล) ให้ใส่ชื่อพารามิเตอร์เหล่านั้นในช่อง "Parameter Group" ถ้ามีค่าลับ (Secret) ให้เลือก Input Source เป็น "secret_configuration" และตั้งชื่อ environment variable ใหม่ (นำค่าจริงไปตั้งใน `.env` นอก source control)
4. ขั้นตอนที่ 4: กรอก Base URL, Endpoint Path, Method, Content Type, Headers
5. ขั้นตอนที่ 5: ทดสอบด้วยข้อมูลจริงหนึ่งค่า แล้วกด "Save and Enable"
6. หากต้องการตั้งค่าขั้นสูง (Response Mapping, Validation, Retry Policy) ให้เปิด Advanced Settings

## Decision Engine Integration — Still Future Work

Nothing in this task wires Business Actions into intent classification, semantic routing, or the Slot Filling Engine. `validate_can_execute()` / `resolve_secret_parameters()` are ready for a future Decision Engine to call, but no such caller exists yet — `Test Action` remains the only way an action executes today.
