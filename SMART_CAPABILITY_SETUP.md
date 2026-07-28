# Smart Capability Setup

The **single, default entry point** for adding any Business Action —
"+ เพิ่มความสามารถให้ AI". Replaces the old "Choose Action Type" picker
(Connect an API / RAG / Tool / Human Handoff / Notification / Workflow /
Webhook / Advanced) as the default flow: the admin never has to know or
choose an internal type — AI detects it automatically from whatever they
paste or describe.

This is a UX-layer unification on top of the existing AI Auto Setup
service (see [AI_AUTO_SETUP.md](AI_AUTO_SETUP.md)) — the backend
(`services/ai_auto_setup_service.py::analyze_capability()`, the
Business Action Registry, Decision Engine, Dynamic Information
Collection, Generic Action Executor) is unchanged in architecture, only
extended additively (type detection fields, question expansion,
per-type Draft rules).

## Unified Setup Flow

```
Click "+ เพิ่มความสามารถให้ AI"
  → ONE screen: "ให้ AI เชื่อมต่อและตั้งค่าให้"
    - Main input: "วางลิงก์ API, cURL, เอกสาร หรืออธิบายสิ่งที่ต้องการ"
    - Optional file upload: "อัปโหลดเอกสารหรือไฟล์ตั้งค่า"
    - Secondary field: "ตัวอย่างคำถามหรือเหตุการณ์" (1–10 examples)
    - Button: "วิเคราะห์และสร้างให้"
  → AI detects the capability type + proposes full structured config
  → (medium confidence) 2–3 business-friendly interpretations to pick from
  → (low confidence) one concise clarification question
  → Business-friendly Review (same screen for every type)
  → Test and Enable
```

No Action Type picker is ever shown in this default path. The "⚙️
Advanced / Other" button next to "+ เพิ่มความสามารถให้ AI" still opens
the old type picker → 5-step Manual Wizard, for developers/platform
admins who want to skip AI entirely — it is a secondary, clearly-labeled
entry, never the default.

Every one of the 7 internal types (API, WEBHOOK, RAG, TOOL,
HUMAN_HANDOFF, NOTIFICATION, WORKFLOW) goes through this **same**
Smart Setup screen — none of them route to the 9-tab editor
automatically.

## Supported Inputs

Endpoint URL, cURL, raw API documentation, OpenAPI/Swagger, Postman
Collection, PDF, Word (.docx), Markdown, plain Text, or a
plain-language requirement in Thai/English (e.g. "ใช้ค้นหาออเดอร์ โดย
ต้องถามรหัสลูกค้าและเลข PO", "ถ้าลูกค้าขอคุยกับเจ้าหน้าที่ ให้ส่งต่อ
Human Handoff", "ค้นหาคำตอบจาก Knowledge Base เรื่องคลังสินค้า"). JSON/
YAML uploads (OpenAPI/Postman) are read as text and appended to the
main input automatically; PDF/Word uploads are attached by filename
with a prompt to also paste key excerpts (this admin UI does not do
binary text extraction client-side — see Limitations).

## Automatic Type Detection

`analyze_capability()` (renamed/extended from `analyze_api()`, which
remains a backward-compatible alias) asks the AI to return
`detected_action_type` (one of the 7 internal types),
`detection_confidence` (`high`/`medium`/`low`), and
`detection_reason`, alongside the full structured proposal — in the
**same** LLM call, no separate classification round-trip. Detection
rules given to the model:

| Signal in the input | Detected type |
|---|---|
| HTTP endpoint, method, cURL, or request body | `API` |
| Wants to *receive* data from an external system (not search) | `WEBHOOK` |
| Wants to search documents/Knowledge Base | `RAG` |
| Wants to calculate/convert/parse internally | `TOOL` |
| Wants to transfer to staff | `HUMAN_HANDOFF` |
| Wants to alert or send information out | `NOTIFICATION` |
| Describes multiple sequential steps | `WORKFLOW` |

The user is **never** asked to pick a type before analysis.

## Confidence and Clarification

- **High** → proceeds straight to the business-friendly Review screen.
- **Medium** → shows 2–3 business-friendly interpretations (e.g. "AI
  เข้าใจว่าคุณต้องการ: 1. เชื่อมต่อ API เพื่อค้นหาข้อมูลออเดอร์ 2.
  สร้าง Webhook เพื่อรับข้อมูลออเดอร์") — never raw internal type labels
  unless Developer Details (Advanced Settings) is opened. Picking one
  sets `proposal.action_type` and proceeds to Review.
- **Low** → shows one concise clarification question; the admin's
  answer is appended to the original input and re-analyzed.

Verified live: SearchDataOrder documentation → `detected_action_type:
API`, `detection_confidence: high` (skips straight to Review).
"ค้นหาคำตอบจาก Knowledge Base เรื่องคลังสินค้า" → `RAG`, `high`. "ถ้า
ลูกค้าขอคุยกับเจ้าหน้าที่ ให้ส่งต่อ" → `HUMAN_HANDOFF`, `high`.

## Business-Friendly Review

Same review screen for every detected type, showing only:
ชื่อความสามารถ, ใช้สำหรับอะไร, ลูกค้าจะถามเมื่อใด (from
`detection_reason`), ตัวอย่างคำถาม (editable chips, remove/expand),
ข้อมูลที่ AI ต้องถามลูกค้า (required customer-provided parameters, with
3 selectable/editable follow-up questions each), ข้อมูลที่ระบบดึงเอง
(secrets/profile-sourced values), เงื่อนไขการทำงาน, ระบบหรือแหล่งข้อมูล
ที่เชื่อมต่อ, ตัวอย่างคำตอบ, ความมั่นใจในการวิเคราะห์, and ⚠ คำเตือนที่
ต้องตรวจสอบ (when the AI flagged any). **Never shown by default**:
Action ID's internal meaning, raw Action Type, Executor internals,
Parameter Source enum values, raw response mapping JSON, Priority, or
category as a technical field, secret resolver mechanics.

One link — "ดูรายละเอียดทางเทคนิค" (also present as "Advanced Settings"
on the Test stage) — saves the current proposal as a Draft and opens
the **unchanged** 9-tab Advanced Editor for that exact record. Verified
live: clicking it opens `#ba-modal` with the saved draft populated.

## Question Enrichment

Entering one example ("เช็ค PO") and clicking "AI ช่วยขยายตัวอย่าง"
calls `services/ai_auto_setup_service.py::expand_example_questions()`
(a small, separate LLM call — never invents examples inside the main
analysis prompt) and shows 4–7 additional phrasings as selectable
checkboxes ("ตรวจสอบสถานะคำสั่งซื้อ", "ช่วยดู PO ให้หน่อยได้ไหม", …),
with **Generate more** to re-run and get a fresh batch. Selected
suggestions merge (deduplicated) into `example_questions`. Verified
live with a real AI call.

## Existing Action Editing

Clicking the normal **Edit** button on any existing action now opens a
lightweight business-friendly summary first (ใช้สำหรับ / สถานะ / ข้อมูล
ที่ต้องถามลูกค้า / ตัวอย่างคำถาม) with action buttons: **Test**,
**Simulate Conversation**, **Edit with AI**, **Advanced Settings**,
**Disable/Enable**, **Delete**. The 9-tab editor is reached **only**
via the explicit "Advanced Settings" button on this summary — never
automatically. "Edit with AI" re-opens Smart Setup pre-filled with the
existing action's name/description as a starting point for a guided
re-analysis (does not auto-save; same Review → Test → Enable flow).

## Uploaded Knowledge Documents

If a non-API-shaped file is uploaded (PDF/Word/Markdown/Text) and the
AI detects `RAG`, Smart Setup asks explicitly: (A) add to Knowledge
Base only, (B) create a Knowledge Search capability only, or (C) both
— **never silently ingests a file into RAG**. Choosing (A) closes
Smart Setup and directs the admin to the Knowledge Base page instead
(this task does not implement the KB ingestion trigger itself — see
Limitations).

## Draft Rules (per detected type)

`registry.validate_can_enable()` (extended, additive) blocks Enable
per type:

| Type | Requirement to Enable |
|---|---|
| API / WEBHOOK | endpoint + method present; required customer parameters have a follow-up question; secret parameters have a `secret_ref`; no ambiguous validation across multiple required fields |
| RAG | `setup_metadata.knowledge_scope` set |
| TOOL | `setup_metadata.tool_name` set (must reference an existing internal tool) |
| HUMAN_HANDOFF / NOTIFICATION | `setup_metadata.triggers` has at least one entry |
| WORKFLOW | `setup_metadata.workflow_steps` has at least one entry |

A `detection_confidence: "low"` proposal additionally **cannot be
enabled** at all until the admin has resolved the clarification and
re-analyzed (enforced in the save route, not just the UI).

## Security

- Secrets redacted (`redact_secrets()`) before anything reaches the
  LLM — unchanged from AI Auto Setup, still applies here since the same
  `analyze_capability()` function is used.
- Raw secrets from a pasted cURL are never stored; `Authorization`
  header values are never displayed anywhere in Review/Test.
- Uploaded documents are never added to the Knowledge Base without
  explicit admin confirmation (A/B/C choice above).
- Destructive-looking endpoints (DELETE methods, etc.) are still gated
  by the same Draft/Enable rules — nothing is auto-enabled.
- No script from an uploaded Postman/OpenAPI file is ever executed —
  file content is treated as plain text input to the LLM analysis only.
- Low-confidence proposals cannot be auto-enabled (enforced server-side).

## Files Changed

`services/ai_auto_setup_service.py` (added `analyze_capability()` +
type-detection schema/prompt + `expand_example_questions()`, kept
`analyze_api()` as an alias), `services/business_action_registry.py`
(added `setup_metadata` column usage + per-type `validate_can_enable()`
rules), `admin/routes.py` (extended analyze/save routes, added
expand-questions route), `admin/templates/business_actions.html`
(unified Smart Setup modal, business-friendly Review, new Edit summary
modal, type picker demoted to an explicit "Advanced / Other" button),
migration `029_smart_capability_setup.sql` (additive `setup_metadata`
column).

## Current Limitations

- PDF/Word text extraction is not done client-side in this admin UI —
  uploading a PDF/Word file attaches its filename as a marker and asks
  the admin to also paste key excerpts into the main input for best
  results. A server-side extraction step is future work.
- The Knowledge Base ingestion path (choice A/C in the upload dialog)
  hands off to the existing Knowledge Base page rather than
  implementing ingestion inline in this task.
- "Simulate Conversation" runs a direct Test Action call, not a full
  Decision Engine turn (no Channel Adapter exists yet to simulate
  through) — this is called out in the UI itself.
- Medium/low confidence interpretation labels are generated per-request
  by the LLM and are not guaranteed word-for-word identical across
  repeated analyses of similar input — acceptable since the admin
  always reviews and confirms before anything saves.
