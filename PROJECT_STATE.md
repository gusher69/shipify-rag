# PROJECT_STATE.md

_Refreshed: 2026-07-30, during the git-prep/handover pass that follows the Semantic Analysis Review & Edit UI sprint. This file tracks live sprint/architecture state; `docs/CURRENT_STATUS.md` is the longer-form feature inventory, `docs/adr/` records individual architecture decisions in full — this file is the fast-orientation summary._

## Current Sprint

**Manual ERP API onboarding verification** (paused mid-flow at the user's request to prepare this handover). Walking `GetDataCustomer` through the full real admin flow step-by-step: import → AI-Guided Setup analysis → Semantic Analysis review → auth/search-field verification → validation-group check (`AT_LEAST_ONE` over CustCode/CustEmail/CustName/CustPhone) → save as Draft/Disabled → configure secrets → non-destructive API test → inspect request/response/mapping/logs/errors → conversation-level test without enabling. Expected: Endpoint Intent LOOKUP, Runtime Operation Type LOOKUP, SecretCode=authentication, the other 4 fields=search fields with an AT_LEAST_ONE group. No side-effect execution, no publish/enable. **Resume at whichever numbered step the user left off at** (see conversation history — this is a live, guided, step-by-step walkthrough, not something to complete unattended).

## Current Architecture

Two independent FastAPI apps sharing one `services/`/`rag/`/`ingestion/` codebase:
- `line_bot.webhook:app` — customer-facing LINE channel (port 8000).
- `admin.routes:app` — Admin Web UI + JSON API (port 8001; `admin.preview_server:app` is the dev-preview variant `.claude/launch.json` uses).

See `docs/ARCHITECTURE.md` for the full data-flow diagrams. The two architecturally significant additions this handover covers, both **uncommitted**:

1. **Semantic API Analysis Engine + Review & Edit UI** (`docs/adr/0002-semantic-engine-and-transform-operation.md`) — a deterministic (non-LLM) classification engine for AI-Guided ERP Setup, with a Detected/Override/Effective UI surfaced in the Summary Card.
2. **Business Action permanent hard delete** (`docs/adr/0001-business-action-hard-delete.md`) — replaced soft-delete with a real `DELETE` + immutable audit log.

Both bridge into the pre-existing Generic Integration Runtime (`services/erp_test_harness.py`, `services/integration_schema_service.py`, `services/integration_contract_service.py`) without modifying its tested classifier/resolver code.

## Major Components

| Component | File(s) | Role |
|---|---|---|
| Business Action Registry | `services/business_action_registry.py` | CRUD + hard-delete + parameter groups + dependent-record accounting |
| AI Auto Setup | `services/ai_auto_setup_service.py` | LLM-driven proposal generation + deterministic post-processing (confidence, similarity, intent classification) |
| Semantic API Analysis Engine | `services/semantic_api_analysis_engine.py` | Deterministic endpoint/field classification, Detected→Override→Effective merge |
| Generic Integration Runtime | `services/erp_test_harness.py`, `integration_schema_service.py`, `integration_contract_service.py` | Product-agnostic test/execution + effective-schema resolution + canonical contract |
| Credential Store | `services/credential_store.py` | Fernet-encrypted secret storage |
| Conversation Form Generator / Strategy Engine | `services/conversation_form_generator.py`, `conversation_strategy_engine.py` | Generates the conversational slot-filling form from a resolved contract/schema |
| UAT Runner | `services/uat_runner.py`, `scripts/run_uat_suite.py`, `admin/templates/uat_dashboard.html` | Regression/UAT suite with run history (`uat_runs/`) |
| Admin templates | `admin/templates/business_actions.html`, `integration_schema_studio.html`, `erp_conversation_tester.html`, `erp_api_explorer.html`, `erp_test_history.html`, `conversation_analytics_dashboard.html`, `uat_dashboard.html` | All server-rendered Jinja2 + inline vanilla JS |

## Completed Features (this uncommitted body of work)

- Embedding-dimension production-incident fix (config precedence + startup/pre-ingestion validation).
- Business Action duplicate-key collision fix + differentiated duplicate-resolution dialog by state (Published/Draft/Soft-Deleted-legacy).
- Business Action hard-delete rework (see ADR 0001) + immutable audit log (migration 034).
- Integration Schema Studio (draft/published/archived versioning, 3-layer effective-schema resolver, provenance tracking) — migration 033.
- Integration Contract Layer (canonical, cached, read-only contract for downstream consumers).
- Generic Integration Runtime / ERP Conversation Tester (`erp_test_harness.py`, migration 031).
- Conversation Form Generator + Conversation Strategy Engine.
- UAT / Regression Test Suite with run history + CLI entry point.
- Semantic API Analysis Engine (see ADR 0002) — Steps 1-9/11/12, removed the old hardcoded `_INTENT_TAXONOMY`.
- TRANSFORM promoted to a first-class runtime operation type (not aliased to CALCULATION).
- Semantic Analysis Review & Edit UI panel in AI-Guided Setup's Summary Card, with live Detected/Override/Effective + dynamic operation-vocabulary dropdowns (`GET /admin/api/integration-contracts/operations`, `GET /admin/api/business-actions/ai-auto-setup/semantic-endpoint-intents`).
- AI-suggestion collision avoidance (avoid reusing phrasing already present in RAG/other Business Actions) + an animated AI-analyze loading state.
- 3 real draft ERP actions onboarded from real documentation: `search_data_shipment_list`, `get_url_product_detail`, `send_line_noti_cs` (all Draft/Disabled, never published).

## Features In Progress

- Manual GetDataCustomer onboarding verification walkthrough (current sprint, above).
- Field Roles / Entity Mapping override UI control (backend supports it, no UI widget yet — `docs/NEXT_STEPS.md` #4).

## Known Limitations

- `tests/test_ai_auto_setup_registry.py::TestDecisionEngineEitherOrderConversation::test_executor_blocks_safely_when_secret_missing` — one known, pre-existing, unrelated failure (Action Executor makes a real network call instead of blocking locally when a required secret is missing). Never introduced by, and not fixed by, any sprint in this body of work. `docs/NEXT_STEPS.md` #1.
- Decision Engine's production wiring into the live LINE path is unconfirmed (`docs/NEXT_STEPS.md` #2).
- The Semantic API Analysis Engine's field-role heuristics are regex/structural and imperfect on truly novel API shapes — this is why the Override mechanism exists; one real bug (a credential field wrongly bucketed into a validation-group suggestion) was found and fixed during manual UI verification, but more such gaps may surface as new real APIs are onboarded.

## Technical Debt

- An entire orphaned OLD AI Auto Setup Review & Edit flow (`baAiRenderReview()`/`#ba-ai-review` and ~8 related functions in `admin/templates/business_actions.html`) is confirmed unreachable but not removed — see `docs/CURRENT_STATUS.md` Technical Debt and `docs/NEXT_STEPS.md` #5.
- `admin/routes.py` is a single ~4700-line file (`docs/NEXT_STEPS.md` #7).
- No migration-tracking table (`docs/NEXT_STEPS.md` #8); currently at migration 034.
- **This entire body of work (everything described above) was uncommitted until this handover pass** — see `HANDOVER.md` for the exact file list. If you're reading this and `git log` doesn't show a recent commit matching this description, the commit from Step 12 of the handover workflow may not have happened yet, or push (Step 13) is still pending explicit user approval.

## Next Sprint

Per `docs/NEXT_STEPS.md`, in priority order: (P1) fix Action Executor's pre-flight secret validation, confirm Decision Engine wiring; (P2) finish manual onboarding verification for all 4 draft actions, add the Field Roles override UI, remove the dead Review & Edit flow, wire the Semantic Business Layer into a real consumer, split `admin/routes.py`, formalize migration tracking, add CI.

## Current Priorities

1. Finish the paused manual `GetDataCustomer` onboarding walkthrough (do not resume automation — wait for the user at each step, per their explicit instruction).
2. Complete this handover: commit (not push) once Step 1-11 of the handover workflow are done; wait for explicit approval before push.
3. Do not start new feature development until the user lifts the "pause new feature development" instruction.

## Recent Architectural Decisions

See `docs/adr/0001-business-action-hard-delete.md` and `docs/adr/0002-semantic-engine-and-transform-operation.md` for full Context/Problem/Decision/Alternatives/Trade-offs/Consequences/Migration/Future-Evolution write-ups.

## Current Canonical Flow

**AI-Guided ERP Setup, end to end:**
```
Admin pastes API definition
  → POST /admin/api/business-actions/ai-auto-setup/analyze
      → ai_auto_setup_service.analyze_capability()
          → secret redaction → LLM proposal → deterministic post-processing
          → semantic_api_analysis_engine.analyze_endpoint() (deterministic, no LLM)
  → Admin UI: baAiRenderSummaryCard() / #ba-ai-summary-card
      → Semantic Analysis panel: baAiRenderSemanticPanel(), overrides via
        POST /ai-auto-setup/semantic-overrides/apply (apply_overrides())
  → POST /admin/api/business-actions/ai-auto-setup/save
      → business_action_registry persists action + setup_metadata.
        {operation_type, semantic_analysis, semantic_overrides}
      → saved as Draft/Disabled unless admin explicitly enables
  → (later) Integration Schema Studio / ERP Conversation Tester / Test API
    all read the SAME persisted setup_metadata.operation_type through
    erp_test_harness.infer_operation_type_with_evidence()'s existing
    tier-2 override — no second runtime classifier anywhere.
```
