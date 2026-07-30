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
   - runs deterministic post-processing (confidence scoring, business/response field mapping, similarity check against existing actions).
   - calls `services/semantic_api_analysis_engine.py::analyze_endpoint()` — a **second, purely deterministic, non-LLM** pass (see "Semantic API Analysis Engine" below) — and attaches its output as `proposal.setup_metadata.semantic_analysis` / `.operation_type`.
3. The Admin UI (`baAiRenderSummaryCard()`, NOT the older `baAiRenderReview()` — see Known Tech Debt) renders a **Review & Edit / Summary Card** page: every field editable inline, secret values shown only as masked previews (never their real value), an Explainability panel (parse summary, capability/entity/intent detection, confidence breakdown), and a **Semantic Analysis panel** (endpoint intent, runtime operation type, auth/required/optional/search-criteria fields, validation groups, business entities, side-effect/confirmation/audit flags, response-mapping status, severity-tagged recommendations) with Detected/Override/Effective controls per field.
4. On save (`POST /admin/api/business-actions/ai-auto-setup/save`), any real secret value is sent once and stored via `services/credential_store.py` (encrypted); the Business Action itself, its parameters, execution config, and `setup_metadata.{operation_type, semantic_analysis, semantic_overrides}` are persisted via `services/business_action_registry.py` to Supabase Postgres. Nothing is auto-enabled — it saves as a draft until the admin explicitly enables it.
5. At runtime, `services/action_executor.py` reads this same registry data to make the actual HTTP call — the AI Auto Setup pipeline never executes anything itself.

## Semantic API Analysis Engine + Generic Integration Runtime (2026-07-29/30 sprint)

A second, deterministic classification/derivation stack sits alongside the LLM-driven AI Auto Setup pipeline above — added to fix a real defect (three previously-disconnected, sometimes-conflicting classification systems) and to make Business Action setup work generically across *any* REST API, not just ERP-specific patterns.

```
Setup-time (never touches a live API):
  services/semantic_api_analysis_engine.py::analyze_endpoint()
    → endpoint_intent (LOOKUP/SEARCH/LIST/DETAIL/TRANSFORM/COMMAND/NOTIFICATION/
       MUTATION/UPLOAD/DOWNLOAD/AUTHENTICATION/HEALTHCHECK/UTILITY/UNKNOWN)
    → field roles (authentication/identifier/search/filter/date_start/date_end/
       enum/limit/pagination/message/url/credential/... — multi-role)
    → validation_groups, operation_safety, response_analysis, recommendations
    → mapped_runtime_operation_type  (bridges into the runtime vocabulary below)
  apply_overrides() — the ONE place Detected → Admin Override → Effective
    merging happens (mirrors services/integration_schema_service.py's existing
    3-layer precedent); reused by both the Review & Edit UI's live preview
    AND the save route.

Runtime (an already-saved Business Action):
  services/erp_test_harness.py::infer_operation_type_with_evidence()
    — the pre-existing, evidence-ranked runtime classifier (its own
    OPERATION_TYPES vocabulary: LOOKUP/SEARCH/LIST/STATUS/CALCULATION/CREATE/
    UPDATE/DELETE/CANCEL/WORKFLOW/NOTIFICATION/NOTIFY/TRANSFORM/UNKNOWN).
    Reads `setup_metadata.operation_type` (tier 2) — this is the ONLY
    integration point with the setup-time engine above; the runtime
    classifier's own code/tests were never modified.
  services/integration_schema_service.py::resolve_effective_integration_schema()
    — 3-layer (runtime default → derived → explicit schema override)
    resolution of operation_type + conversation behavior + field display.
  services/integration_contract_service.py::describe_integration()
    — the canonical, cached, read-only "contract" every downstream
    consumer (ERP Conversation Tester, Playground, future Multi-ERP
    orchestration) reads instead of re-deriving anything itself.
  services/erp_test_harness.py::run_erp_test() — the Generic Integration
    Conversation Runtime: intent_param / simulation / live test modes,
    product-agnostic (adding a new integration requires zero changes here).
```

Key design decision: the setup-time engine's richer vocabulary (14 values, includes `TRANSFORM`, `DETAIL`, `COMMAND`, etc.) is **deliberately not the same enum** as the runtime's vocabulary — they're bridged by `map_to_runtime_operation_type()`, not unified, so the well-tested runtime classifier's existing vocabulary/tests/Decision-Engine-adjacent behavior stay untouched. See `docs/adr/0002-semantic-engine-and-transform-operation.md`.

## Frontend / Backend / Database / External Integration boundaries

- **Frontend**: server-rendered Jinja2 (`admin/templates/*.html`) + inline vanilla JS. No SPA framework, no build step, no `package.json`. All admin interactivity is `fetch()` calls to `admin/routes.py` JSON endpoints.
- **Backend**: FastAPI, two independent apps (`admin.routes:app`, `line_bot.webhook:app`) sharing the same `services/`, `rag/`, `ingestion/` Python packages. `config.py` is the single place environment variables are read.
- **Database**: Supabase Postgres (relational tables + pgvector extension for embeddings) — no ORM, plain SQL migrations in `migrations/`.
- **External integrations**: OpenAI (LLM/embeddings, swappable to Azure/Groq/OpenRouter), LINE (channel + Notify), Google Drive (sync source), Supabase Storage or S3 (file storage), the customer's ERP (read-only, via `erp/bridge.py` or a generic Business Action HTTP call).

## Why Business Actions exist as a layer

Rather than writing a bespoke integration module per ERP endpoint per customer, every ERP/API call is described as data (endpoint, method, headers, parameters, auth, response mapping) in the `business_actions*` tables and executed generically by `services/action_executor.py`. This is the concrete implementation of the Platform-First principle in `CLAUDE.md`: onboarding a new API is a configuration exercise (optionally AI-assisted), not a code change.
