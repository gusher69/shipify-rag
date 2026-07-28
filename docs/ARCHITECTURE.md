# Architecture

## Components

```
┌─────────────────────┐        ┌──────────────────────────────┐
│   LINE Official      │◄──────►│  line_bot/webhook.py          │
│   Account (customer) │        │  (FastAPI app, port 8000)     │
└─────────────────────┘        │  - intent.py (intent+lang)    │
                                 │  - tone.py (reply tone)       │
                                 │  - message_adapter.py         │
                                 └───────────┬───────────────────┘
                                             │
                          ┌──────────────────┴───────────────────┐
                          │        services/decision_engine.py     │
                          │  routes to ERP, RAG, or both            │
                          └──────┬───────────────────────┬─────────┘
                                 │                       │
                    ┌────────────▼───────────┐  ┌────────▼─────────────┐
                    │ services/action_executor │  │  rag/searcher.py       │
                    │  + business_action_       │  │  + hybrid_scoring.py   │
                    │    registry.py             │  │  + query_understanding│
                    │  (config-driven ERP call)  │  │  (Supabase pgvector)  │
                    └────────────┬───────────┘  └────────┬─────────────┘
                                 │                       │
                    ┌────────────▼───────────┐  ┌────────▼─────────────┐
                    │  erp/bridge.py           │  │  Supabase Postgres    │
                    │  (read-only PHP/MySQL)   │  │  (pgvector, tables)   │
                    └─────────────────────────┘  └───────────────────────┘

┌───────────────────────────────────────────────────────────────────────┐
│                  admin/routes.py  (FastAPI app, port 8001)             │
│  Knowledge Base · ERP Integration (Business Actions) · AI Playground   │
│  Prompt Studio · AI Policies · Evaluation · Production Validation      │
│  ── uses the SAME services/ layer as the customer-facing path ──      │
└───────────────────────────────────────────────────────────────────────┘

External: OpenAI / Azure OpenAI / Groq / OpenRouter (LLM+embeddings)
          Google Drive API (nightly sync, ingestion/gdrive_sync.py)
          Supabase Storage or S3 (file storage, storage/)
          LINE Notify (handoff alerts)
```

## Data Flow — Customer Message (LINE)

1. LINE POSTs to `line_bot/webhook.py: POST /webhook` (signature-verified with `LINE_CHANNEL_SECRET`).
2. `line_bot/intent.py` classifies intent/sentiment/language (LLM call).
3. `services/decision_engine.py` decides the source(s):
   - live/record data → `services/action_executor.py` executes a configured **Business Action** (looked up via `services/business_action_registry.py`), which calls out to `erp/bridge.py` (or any other configured HTTP API) using credentials from `services/credential_store.py` (or a legacy env-var secret).
   - policy/FAQ data → `rag/searcher.py` performs hybrid (vector + keyword + heading) retrieval against `knowledge_chunks` in Supabase pgvector, using `services/embedding_service.py` for the query embedding.
4. `line_bot/tone.py` composes the final reply from whatever was retrieved/returned.
5. If confidence is below `CONFIDENCE_THRESHOLD`, or a handoff-triggering signal is detected, the conversation is escalated via LINE Notify instead of auto-replying.
6. `profiles/manager.py` updates the customer's segment/profile.

## Data Flow — Admin Configures a New ERP Integration

1. Admin pastes a cURL/API description into the AI-Guided ERP Setup wizard (`admin/templates/business_actions.html` → `POST /admin/api/business-actions/ai-auto-setup/analyze`).
2. `services/ai_auto_setup_service.py::analyze_capability()`:
   - runs `services/secret_detector.py` **locally, before any LLM call**, to find and redact secret-shaped values (never sent to the LLM in plaintext).
   - calls the LLM (via `services/llm_service.py`) to propose a structured configuration.
   - runs deterministic post-processing (confidence scoring, business/response field mapping, similarity check against existing actions) — none of this is a second LLM call.
3. The Admin UI renders a **Review & Edit** page: every field editable inline, secret values shown only as masked previews, a semantic classification layer (canonical names, detected entities/intents, KB recommendations) computed client-side from the same response.
4. On save (`POST /admin/api/business-actions/ai-auto-setup/save`), any real secret value is sent once and stored via `services/credential_store.py` (encrypted); the Business Action itself, its parameters, and execution config are persisted via `services/business_action_registry.py` to Supabase Postgres. Nothing is auto-enabled — it saves as a draft until the admin explicitly enables it.
5. At runtime, `services/action_executor.py` reads this same registry data to make the actual HTTP call — the AI Auto Setup pipeline never executes anything itself.

## Frontend / Backend / Database / External Integration boundaries

- **Frontend**: server-rendered Jinja2 (`admin/templates/*.html`) + inline vanilla JS. No SPA framework, no build step, no `package.json`. All admin interactivity is `fetch()` calls to `admin/routes.py` JSON endpoints.
- **Backend**: FastAPI, two independent apps (`admin.routes:app`, `line_bot.webhook:app`) sharing the same `services/`, `rag/`, `ingestion/` Python packages. `config.py` is the single place environment variables are read.
- **Database**: Supabase Postgres (relational tables + pgvector extension for embeddings) — no ORM, plain SQL migrations in `migrations/`.
- **External integrations**: OpenAI (LLM/embeddings, swappable to Azure/Groq/OpenRouter), LINE (channel + Notify), Google Drive (sync source), Supabase Storage or S3 (file storage), the customer's ERP (read-only, via `erp/bridge.py` or a generic Business Action HTTP call).

## Why Business Actions exist as a layer

Rather than writing a bespoke integration module per ERP endpoint per customer, every ERP/API call is described as data (endpoint, method, headers, parameters, auth, response mapping) in the `business_actions*` tables and executed generically by `services/action_executor.py`. This is the concrete implementation of the Platform-First principle in `CLAUDE.md`: onboarding a new API is a configuration exercise (optionally AI-assisted), not a code change.
