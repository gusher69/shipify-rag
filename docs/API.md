# API Reference

There are **two separate FastAPI applications** exposing HTTP endpoints:

1. `admin.routes:app` — the Admin Web UI + its backing JSON API (`/admin/...`, ~135 routes). Session-cookie authenticated.
2. `line_bot.webhook:app` — the LINE Messaging API webhook (`POST /webhook`). LINE-signature authenticated.

This document covers the important/most-used endpoints, not an exhaustive list — see `admin/routes.py` and `line_bot/webhook.py` for the full route table.

## Authentication

### Admin app
- `POST /admin/login` — form login (`username`, `password` against `ADMIN_USERNAME`/`ADMIN_PASSWORD`), sets a signed session cookie (`SESSION_SECRET`).
- `GET /admin/logout` — clears the session.
- Every `/admin/...` route checks the session cookie via an `auth(request)` guard at the top of the handler; an invalid/missing session redirects to `/admin/login`.
- There is **no separate API token/bearer auth** for the admin API today — it's cookie-session only, intended for same-origin browser use.

### LINE webhook
- `POST /webhook` verifies the `X-Line-Signature` header against `LINE_CHANNEL_SECRET` (LINE SDK signature validation) before processing.

## Business Action Center (`/admin/api/business-actions/...`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/api/business-actions` | List all Business Actions |
| POST | `/admin/api/business-actions` | Create a Business Action manually (non-AI path) |
| GET | `/admin/api/business-actions/{id}` | Get one Business Action (full detail) |
| DELETE | `/admin/api/business-actions/{id}` | Delete (soft-delete) a Business Action |
| GET | `/admin/api/business-actions/{id}/enable-check` | Validate whether an action has everything required to be enabled |
| POST | `/admin/api/business-actions/{id}/enabled` | Enable/disable |
| POST | `/admin/api/business-actions/{id}/test` | Execute the configured request directly (bypasses the AI/Decision Engine — for admin testing only) |
| POST | `/admin/api/business-actions/{id}/execute` | Execute via the normal runtime path (used by the Decision Engine / Playground) |
| POST | `/admin/api/business-actions/{id}/duplicate` | Clone an existing action |
| GET | `/admin/api/business-actions/{id}/export` | Export as JSON |
| POST | `/admin/api/business-actions/import` | Import a previously-exported JSON |
| PUT | `/admin/api/business-actions/{id}/parameters` \| `/parameter-groups` \| `/execution` \| `/response-mapping` \| `/tags` \| `/examples` \| `/validation` | Update individual sub-resources from the Advanced Editor |

### AI Auto Setup ("AI-Guided ERP Setup")

| Method | Path | Purpose |
|---|---|---|
| POST | `/admin/api/business-actions/ai-auto-setup/analyze` | Send a cURL/API description + business purpose → returns a structured, AI-generated proposal (never saved yet) |
| POST | `/admin/api/business-actions/ai-auto-setup/detect-endpoints` | Detect multiple endpoints inside a pasted Postman collection / OpenAPI doc |
| POST | `/admin/api/business-actions/ai-auto-setup/suggest-questions` / `expand-questions` | AI-generated example customer questions for a capability |
| POST | `/admin/api/business-actions/ai-auto-setup/save` | Persist the admin-reviewed proposal as a Business Action (creates or updates; handles duplicate-key detection) |

**Request shape (analyze)**:
```json
{"user_input": "curl --location '...' --data-urlencode 'SecretCode=...' ...",
 "business_purpose": "Look up customer profile by customer code",
 "search_info": "", "example_questions": []}
```
**Response shape (analyze)** — key fields: `ok`, `proposal` (the full structured Business Action draft), `detected_credentials` (masked-only secret metadata), `confidence` (per-section scores), `response_field_mapping`, `errors`.

## Credential Store (`/admin/api/credentials/...`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/api/credentials` | List credentials (metadata + masked preview only — never the raw value) |
| POST | `/admin/api/credentials` | Create a new encrypted credential |
| PUT | `/admin/api/credentials/{key}` | Update a credential's value (write-only — new value in, nothing sensitive out) |
| POST | `/admin/api/credentials/{key}/rotate` | Rotate the stored value |
| POST | `/admin/api/credentials/{key}/enable` \| `/disable` \| `/revoke` | Lifecycle state changes |
| POST | `/admin/api/credentials/{key}/test` | Test-resolve a credential reference (used by a Business Action) |
| DELETE | `/admin/api/credentials/{key}` | Delete, only if unused by any Business Action |

**A raw secret value is never returned by any GET/list endpoint** — only `masked_preview` (e.g. `********-777`).

## LINE Webhook

| Method | Path | Purpose |
|---|---|---|
| POST | `/webhook` | LINE platform → this endpoint on every customer message/event |

## External APIs This System Calls

| Service | Used for | Client module |
|---|---|---|
| OpenAI Chat + Embeddings API | LLM responses, embeddings | `services/llm_service.py`, `services/embedding_service.py` |
| Azure OpenAI / Groq / OpenRouter | Alternate LLM providers | `services/llm_service.py` |
| Supabase REST/Postgres | All relational + vector data | `supabase-py` client, configured in `config.py`/`admin/routes.py:get_sb()` |
| Supabase Storage (or S3) | File storage for uploaded knowledge documents | `storage/supabase_storage.py`, `storage/s3_storage.py` |
| LINE Messaging API | Send replies, verify webhook signature | `line-bot-sdk` |
| LINE Notify | Human handoff alerts | direct HTTP call, `LINE_NOTIFY_TOKEN` |
| Google Drive API | Nightly knowledge-base sync | `ingestion/gdrive_sync.py` |
| Customer ERP (PHP/MySQL) | Live order/stock/customer data, OR any Business-Action-configured HTTP API | `erp/bridge.py`, or generically via `services/action_executor.py` |

## Error Response Convention

Admin API endpoints return `{"ok": false, "error": "<message>"}` (or `{"ok": false, "success": false, "error": "..."}` on the Business Action save endpoint) with an appropriate 4xx/5xx status — raw exceptions/stack traces are never forwarded to the client (see the duplicate-key/DB-error handling in `admin/routes.py`'s save endpoint for the pattern to follow when adding new error paths).
