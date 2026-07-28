# AI-Assisted API Auto Setup

**This service is now used behind the unified [Smart Capability Setup](SMART_CAPABILITY_SETUP.md) UX** — "+ เพิ่มความสามารถให้ AI" is the default entry point for every Business Action type (not just API), with automatic type detection layered on top of this same analysis engine. Everything below still applies to the underlying service/schema.

**Secrets detected during analysis are now stored in the [Credential Store](CREDENTIAL_STORE.md)** (`credential_store` input source) instead of requiring a new `.env` variable per secret — see that document for the full Paste-and-Use flow.

Makes registering a new ERP/API integration in the Business Action Center
approachable for a normal admin — no need to understand parameters,
input sources, validation, secret references, or execution config
up front. AI proposes a structured configuration; the admin reviews,
edits, and approves before anything is saved. **AI is used only during
configuration** — at runtime, the Decision Engine / Information
Collection Engine / Generic Action Executor all read exclusively from
the Business Action Registry, never from AI or free-text documents.

## The 3-Stage Flow

Replaces the old 5-step manual wizard as the **default** experience for
"+ New Business Action → 🔌 Connect an API". The old 5-step Manual
Wizard and the Advanced Editor (9 tabs) are both **unchanged and still
reachable** — via "ตั้งค่าด้วยตนเองแทน (Manual Wizard)" on Stage 1, or
"ดูรายละเอียดทางเทคนิค" / "Advanced Settings" from any later stage.

1. **Add API** — paste an endpoint, cURL command, example request, API
   documentation, or upload an OpenAPI/Swagger/Postman file; describe
   the business purpose in one line; give 1–10 example customer
   questions. Nothing technical (Action ID, Category, Parameter Source,
   Executor, Validation, Response Mapping, Secret Reference) is shown
   here.
2. **Review AI Suggestions** — a business-friendly summary: what it's
   called, what it's for, what customers might ask, what information
   needs to be asked from the customer (with 3 selectable/editable
   follow-up question options per parameter), what the system handles
   itself (secrets), the API endpoint, and how many secrets need
   configuring. Admin can edit the Action ID (a hint shows when it
   matches an existing action — the save will UPDATE it, not
   duplicate), question choices, and low-confidence validation patterns.
3. **Test and Enable** — a test form generated from the inferred
   parameters (secrets are never shown/requested here). Test Connection
   saves as a Draft first, then runs the real Test Action call.
   Buttons: Save Draft, Test Connection, Enable, Edit, Advanced
   Settings.

## Supported Inputs

Endpoint URL, cURL command, example request/response, free-text API
documentation, or an uploaded OpenAPI/Swagger/Postman JSON/YAML file —
all handled as plain text sent to the AI analysis step (no dedicated
parser exists or is needed; the LLM does the extraction). Files are
read client-side and appended to the same text input.

## AI Analysis

`services/ai_auto_setup_service.py::analyze_api()` — calls the LLM
**exclusively** through the existing `services/llm_service.py::get_llm_service()`
abstraction (never a direct provider SDK call from the admin route, per
this codebase's own rule that `llm_service.py` is the only place
allowed to do that).

### Secret Redaction (before anything reaches the AI)

`redact_secrets()` replaces any `Authorization`/`X-Api-Key`/`SecretCode`/
`Bearer <token>`/`password=`/etc. **value** with `[REDACTED]`, preserving
header/parameter **names** so the AI can still correctly classify them
as secrets and propose an environment-variable reference — it never
sees a real value. Verified live: a fake `Bearer sk-...` token in a
pasted cURL command never reached the LLM and never appeared in the
AI's response.

### Structured Proposal Schema

The AI is instructed (system prompt) to respond with **only** JSON
matching a fixed schema (`REQUIRED_TOP_LEVEL_KEYS`/`REQUIRED_PARAMETER_KEYS`
in `ai_auto_setup_service.py`) — action identity/description/category,
execution (method/base_url/endpoint_path/content_type/headers), and
per-parameter: name, display_name, required, input_source, secret_ref,
example_value, validation_type, validation_pattern, validation_confidence,
follow_up_options; plus parameter_groups, keywords, example_questions,
response_mapping, and a customer-facing response template.

`validate_proposal_schema()` validates this **server-side** before it
is ever shown to the admin — checks required keys are present, enum
fields (`action_type`, `http_method`, group `rule`, `validation_confidence`)
are valid, and that any `secret_configuration` parameter has a
`secret_ref`. **The AI's raw response is never trusted or auto-saved**
— a schema failure is surfaced as an error, never silently coerced.

### Parameter Source Inference

The AI proposes `input_source` itself; `infer_input_source()` is a
deterministic fallback/validator used to backfill a missing value —
`SecretCode`/`Authorization`/`ApiKey`/`Token`/`ClientSecret`/`Password`-
shaped names → `secret_configuration`; `line_user_id`/`customer_profile_id`-
shaped names → `customer_profile`; everything else → `customer_message`
(never overrides a plausible AI answer, only fills gaps).

### Required Parameter Inference (AND vs. AT_LEAST_ONE)

The AI is explicitly instructed: if every parameter must be present
together (e.g. `SearchDataOrder`'s `CustCode` **and** `OrderCode`), mark
each `required: true` independently and create **no** group. If several
parameters are alternatives (e.g. `GetDataCustomer`'s `CustCode`/
`CustEmail`/`CustName`/`CustPhone` — any one suffices), propose one
`AT_LEAST_ONE` group. Verified live for both cases.

### Validation Inference (the actual fix behind "distinguish C00001 from PO202601001")

This closes a real platform gap: before this task, two ungrouped
required parameters with the same generic shape could only be told
apart by which one happened to be asked about first — a customer
volunteering the "wrong" one's value first would get silently
misattributed. Fixed with:

- **New DB columns** (migration `028_ai_auto_setup_validation.sql`,
  purely additive): `business_action_parameters.validation_pattern`,
  `min_length`, `max_length`, `follow_up_options`, `validation_confidence`;
  `business_actions.setup_source`, `is_draft`.
- **`services/decision_engine.py`**: `_resolve_parameter_validator()` now
  builds each parameter's validator from its own `validation_pattern`
  (regex) + `min_length`/`max_length` when configured, falling back to
  the existing fixed validator table (`phone_number`/`email`/`non_empty`)
  and the shared generic-identifier shape otherwise — nothing here
  redesigns Contextual Slot Binding, it only extends the existing
  per-parameter validator resolution the Decision Engine refactor
  already introduced.
- Both the **ungrouped required** binding loop and the **group member**
  binding loop now try the most-specific validator first (a configured
  pattern, or a non-generic `validation_type`) before a permissive
  catch-all — this is what correctly routes `PO202601001` to `OrderCode`
  and `C00001` to `CustCode` regardless of which one the customer
  mentions first.
- **`_bind_all_from_message()`** (new) — binds **as many** parameters as
  a single message actually provides (not just one), by repeatedly
  re-invoking the binder against a growing "collected" dict and
  excluding values already consumed, until no more progress is made.
  This is what makes "เช็ค PO202601001 ของลูกค้า C00001" bind **both**
  values in a single turn without asking again.

When the AI can't confidently produce a distinguishing pattern,
`validation_confidence: "low"` is set — the Review screen shows a
warning and an editable pattern field, and `registry.validate_can_enable()`
**blocks Enable** (not just a soft warning) when more than one required
askable parameter shares a low-confidence pattern.

## Review and Approval

Nothing from AI analysis is saved automatically. The admin can edit,
before saving: name, description, category, example questions,
required/optional status, AND/AT_LEAST_ONE grouping, parameter display
names, input sources, follow-up questions (pick one of 3 AI-generated
options, or write a custom one), validation patterns, secret reference
names, response mapping, and the customer-facing response template.

## Draft vs. Enabled Actions

`services/business_action_registry.py::validate_can_enable()` — a new,
explicit gate checked only by the AI Auto Setup save route (never by
the existing Advanced Editor's Enable/Disable toggle, which stays
exactly as it was — see Regression note below). Blocks Enable when:
endpoint or HTTP method missing, a required parameter has no name, a
secret parameter has no `secret_ref`, a required askable parameter has
no follow-up question (`description` or `follow_up_options`), or more
than one required askable parameter shares an ambiguous (low-confidence)
validation pattern. An incomplete action is **always** saved as Draft
(`is_draft=True`, `enabled=False`) regardless of what the admin
clicked — it is never silently enabled.

**Important behavior**: saving/testing an action that is **already
live** (e.g. re-verifying `search_data_order` through Auto Setup)
never disables it — only a brand-new action defaults to
draft/disabled. This was found and fixed during live verification (see
Limitations).

## Advanced Settings

Fully unchanged — the same 9-tab editor (`#ba-modal`), same field IDs,
same save endpoints. AI Auto Setup writes to the exact same
`business_action_parameters`/`business_action_execution`/etc. tables
via the same `BusinessActionRegistry` methods the Advanced Editor and
Manual Wizard already use — there is no separate "AI-managed" storage.

## Current Limitations

- No dedicated cURL/OpenAPI/Postman parser exists — the LLM does all
  extraction from raw pasted text. Works well in practice (verified
  live) but isn't guaranteed structurally correct for very large/complex
  specs; the Review screen's editability is the safety net.
- `follow_up_options` selection is stored by copying the chosen text
  into the existing `description` field (the field the Decision Engine
  already reads as a manual override) — there's no separate "selected
  index" column; editing `description` directly in Advanced Settings
  changes the live question exactly as before.
- Continuation resolution can occasionally need a tiebreak when two
  Business Actions generate an identical auto-question (pre-existing
  limitation from the Decision Engine's own conversation-continuation
  design, unrelated to this task — see `DECISION_ENGINE.md`).
- The AI's proposed `secret_ref` name is a suggestion only — always
  cross-check it against any existing `.env` variable name before
  saving, especially when updating an existing action (verified live:
  the AI proposed a slightly different name than the one already
  configured for `search_data_order`; the admin must reconcile this in
  Review before saving, same as any other AI suggestion).
