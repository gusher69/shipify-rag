# PROJECT_STATE.md

_Refreshed: 2026-08-10, Local Cleanup + Deployment Freeze pass (customer UAT deployment target). Everything below the "Current Sprint (2026-08-07)" heading is PRIOR content — kept for history, but superseded by this section for anything that overlaps. `docs/CURRENT_STATUS.md` is the longer-form feature inventory; this file is the fast-orientation summary._

## Current Sprint (2026-08-10) — Deployment Freeze

**Status: READY TO DEPLOY** (pending explicit approval to commit/push/deploy — none of those have happened).

**Database architecture (verified this pass)**: managed Supabase Postgres + pgvector, reached via `supabase-py` (`SUPABASE_URL`/`SUPABASE_SERVICE_KEY`) for every table, and a direct `psycopg2`/`SUPABASE_DB_URL` connection only for one-off migration application. Supabase Storage is configured (`SUPABASE_STORAGE_BUCKET=knowledge-files`) but **`STORAGE_PROVIDER` is currently `local`** on this dev machine — knowledge attachments (including the 18MB ZWIZ.AI PDF flagged for removal, see below) are saved under the local `knowledge/` directory, not Supabase Storage. This is the one piece of local-machine-only state found during this pass; the RAG vector data itself (`knowledge_chunks.embedding`) lives entirely in Supabase and needs no export. Credential Store (`services/credential_store.py`, Fernet-encrypted, table `integration_credentials`) and the full Business Action registry are 100% Supabase-table-backed. **A customer-server deployment can point straight at this same managed Supabase project — no export/import needed for the database itself** (see `DEPLOYMENT_CHECKLIST.md`); switching `STORAGE_PROVIDER` to `supabase` before/at deploy is recommended so attachment files aren't left behind on this machine.

**ERP status — all 8 endpoints, final**:
1. `getdatacustomer` — ✅ enabled, production-ready.
2. `searchdataorder` — ✅ enabled, production-ready.
3. `searchdatashipment` — ✅ enabled, production-ready.
4. `searchdatatracking` — ⛔ **disabled**, classification `CUSTOMER_API_DEFECT` — every real `Tracking` value (freshly chained from live `SearchDataShipmentList` output, re-verified multiple times including this pass) produces an HTTP 500 (`Trying to get property 'type_bill' of non-object`) on the customer's own API. Needs the customer's backend team, or one known-good `CustCode`/`Tracking` pair confirmed to succeed when called directly. Not a Shipify-side defect; not a deployment blocker (the other 7 endpoints are fully functional).
5. `searchdataorderlist` — ✅ enabled, production-ready. Generic semantic parameter inference (natural-language `Latest`/`POStatus` filters) verified working.
6. `searchdatashipmentlist` — ✅ enabled, production-ready. Same semantic inference (`Latest`/`BillStatus`) verified working.
7. `geturlproductdetail` — ✅ enabled, production-ready.
8. `sendlinenotics` — ✅ **enabled**, classification `ENABLED_CONFIGURED_READY_FOR_UAT_LIVE_TEST_PENDING`. A generic, metadata-driven confirmation gate (`services/decision_engine.py::_requires_confirmation`, keyed off `setup_metadata.operation_type`) blocks ANY COMMAND-type action — not just this one — from reaching the Action Executor without an explicit `context={"confirmed": True}` signal for that turn. The LINE webhook (`line_bot/webhook.py`) now persists this pending-confirmation state (`pending_confirmations` table, migration 036, tenant/channel/conversation-scoped, 5-minute default expiry, never stores SecretCode) and recognizes natural confirm/cancel replies ("ยืนยัน"/"ไม่ต้อง"/"yes"/"cancel"/etc. — `services/pending_confirmation_service.py::classify_confirmation_reply`). **No real notification has ever been sent** — every verification pass (backend + webhook-level, scenarios A–G) used a mocked HTTP call. The first real send is a deliberate, explicitly-confirmed test to be performed after deployment.

**Decision Engine ↔ LINE webhook wiring**: `line_bot/webhook.py` DOES route real LINE traffic through `DecisionEngine.decide()` today (`DECISION_ENGINE_LIVE_ROUTING` defaults to `true` in `config.py`) — this resolves `docs/NEXT_STEPS.md` P0 item A, which was accurate as of 2026-08-02 but is now stale; update that file's status if you're reading this from an older copy. Hybrid routing (`_handle_hybrid_turn`) and the generic confirmation gate are both live on this same path, not Playground-only.

**Knowledge Base cleanup — APPLIED (2026-08-10)**: 15 of 17 `knowledge_files` rows were unrelated vendor/demo/internal-ops content — 6 Allianz health-insurance document rows (3 distinct PDFs, each uploaded twice), 2 `Dew Dev Ops.txt` (unrelated SSH/pgAdmin/Git notes for a different project, "Buzzebees"), 1 `สัญญาว่าจ้างพัฒนาระบบ_CONTRACTORSHOP.pdf` (a website contract for an unrelated company), 2 hosting-quotation PDFs, 2 raw `logs_live_*.txt` operational log dumps, 1 `requirements (1) (1).txt` (a Python `requirements.txt`, not knowledge content), and 1 `(TH) ZWIZ.AI_SME 2023 ver.1.pdf` (a competing AI vendor's own marketing brochure, confirmed to have leaked into a live customer-facing answer once). Approved by the user and disabled: `is_active = false` set on both `knowledge_files` and `knowledge_chunks` for all 15 (the `match_knowledge_chunks` RPC's `WHERE kc.is_active = true AND kf.is_active = true` clause is what actually gates retrieval — confirmed via the migration 001 SQL, not guessed). **Not hard-deleted** — fully reversible. Only `AI Knowledge Master.xlsx` and `AI Knowledge Master (1).xlsx` remain active. Verified post-cleanup: the original RAG test query plus 4 adversarial queries (weather small talk, health insurance, sync logs, the CONTRACTORSHOP contract) all now correctly retrieve ONLY `AI Knowledge Master (1).xlsx` or honestly say "not in the knowledge base" — zero contamination leakage. No re-embedding was needed (disabling a file doesn't affect the survivors' existing vectors).

**UAT test accounts (customer-provided, real ERP data, keep)**: `FT1004`, `SP1008`, `FT3182`, `SP1014` (`SP1014` has the richest data and is used as the default in most test scenarios). These live in the customer's own ERP, not in Shipify's Supabase project — nothing to migrate for them.

**Regression**: 1726/1726 passing, `scripts/preflight.py` full PASS (18/18 environment checks), zero secrets printed by either.

**Local servers running this pass**: Admin (`admin.routes:app`) on `:8010`, LINE webhook (`line_bot.webhook:app`) on `:8000` — both via `.claude/launch.json`'s `shipify-admin-real`/`line-webhook` profiles.

---

## Current Sprint (2026-08-07) — historical, superseded by 2026-08-10 above

**ERP Completion + Deployment Readiness Day** — target: real ERP APIs working end-to-end (Business Actions → Decision Engine → AI Playground → RAG+ERP Hybrid), zero regressions, deployment-ready package for tomorrow's customer server deploy.

**ERP status — all 8 real endpoints from the customer's Postman collection** (`AI Chat Web Service API`, https://documenter.getpostman.com/view/10704358/2sBY4PNLFN — confirmed via the collection's own sidebar listing; the detail pages themselves would not render through browser automation, so parameter discovery for the still-blocked endpoints used the same live, read-only probe technique as the working ones, never guessed field names):

1. `getdatacustomer` — ✅ production-ready. All 4 real codes, Decision Engine + full Playground (RAG/ERP/Auto/Hybrid) verified.
2. `searchdataorder` (single PO lookup, param `OrderCode`) — ✅ onboarded + real 200 today, using a real `OrderCode` chained from `searchdataorderlist`'s own output. Minor known limitation: natural-language routing sometimes prefers `searchdataorderlist` over this one when phrasing doesn't clearly signal "one specific order" — not a blocking defect, not fully resolved today (time-boxed).
3. `searchdatashipment` (single shipment lookup, param `ShipmentCode`) — ✅ onboarded + real 200 today, same chaining pattern. Same minor routing-overlap note as #2.
4. `searchdatatracking` (lookup by China tracking number, param `Tracking`) — ⚠️ endpoint confirmed real and param name likely correct (a real 500, not the generic 400 "please provide" error, once `Tracking` is supplied) — but every real `TrackingCH` value from 5 different shipments hit the same backend 500 (`Trying to get property 'type_bill' of non-object`). This looks like a live bug on the customer's own API for this endpoint, not something fixable from our side. **Not onboarded — flagging for the customer's dev team.**
5. `searchdataorderlist` — ✅ production-ready (see below for the P0 bug fixed today).
6. `searchdatashipmentlist` — ✅ production-ready, same fix applied.
7. `geturlproductdetail` — ✅ production-ready (2026-08-09). Real param name `URL` confirmed via the customer's pasted Postman spec (8 earlier guesses had all failed). Real 200 with a live 1688.com URL, honest 400 on an invalid domain. Found+fixed a real bug: the generic identifier extractor was mangling the full URL down to a substring ("1688") — fixed by routing `URL` through the existing generic `system_generated`/`_extract_system_values()` mechanism instead (already built for exactly this, just not previously wired to accept uppercase `URL` as a key, and not previously passed through by the ERP Test Harness's live-mode call — both fixed, both regression-tested).
8. `sendlinenotics` — ⏸️ onboarded but deliberately **disabled** (2026-08-09). Real param name `Message` confirmed via the customer's spec. Credential resolution verified, request construction verified, disabled-action safety gate verified as REAL enforcement (not just a metadata flag) — `ActionExecutor.execute()` refuses to run it. **Stopped before any real send — needs your explicit approval before enabling + sending the one test message.**

**Real bug found and fixed today**: `_NON_ASKABLE_INPUT_SOURCES` (`services/action_selection_primitives.py`) only excluded `secret_configuration`/`credential_store` from "things the customer can be asked for" — missing `fixed_configuration` and `system_generated`. This silently blocked `searchdataorderlist`/`searchdatashipmentlist` from ever completing (the `Latest=5` fixed-config parameter kept showing up as "still need to ask about this"). Fixed, regression-tested (`test_required_fixed_configuration_parameter_never_blocks_completion`).

**Also fixed today**: a `category` collision — `searchdataorderlist`/`searchdatashipmentlist` were both configured with `category="customer"`, which collides with the LEGACY workflow taxonomy (`services/slot_filling_engine.py::_INTENT_ORDER`) and was silently outscoring `getdatacustomer` for customer-info questions via the `+3.0` workflow-match bonus in `search_candidate_actions()`. Renamed to `"Customer Order Retrieval"`/`"Customer Shipment Retrieval"` (matching `getdatacustomer`'s own non-colliding naming convention).

See the end-of-session report for this sprint (conversation history, 2026-08-07) for the full matrix/decision-engine/playground/regression/deployment details — not duplicated here to avoid drift between two copies.

---

## 2026-07-30 snapshot (historical — superseded by the above)

_Refreshed: 2026-07-30, during the git-prep/handover pass that follows the Semantic Analysis Review & Edit UI sprint._

**Manual ERP API onboarding verification** (paused mid-flow at the user's request to prepare this handover). Walking `GetDataCustomer` through the full real admin flow step-by-step: import → AI-Guided Setup analysis → Semantic Analysis review → auth/search-field verification → validation-group check (`AT_LEAST_ONE` over CustCode/CustEmail/CustName/CustPhone) → save as Draft/Disabled → configure secrets → non-destructive API test → inspect request/response/mapping/logs/errors → conversation-level test without enabling. Expected: Endpoint Intent LOOKUP, Runtime Operation Type LOOKUP, SecretCode=authentication, the other 4 fields=search fields with an AT_LEAST_ONE group. No side-effect execution, no publish/enable.

**This describes a sprint that has long since completed and been superseded** — `getdatacustomer` is now production-ready (see Current Sprint above), and substantial additional work (Decision Engine production wiring, Hybrid AI, LINE OA live integration, Phase 3 Conversation Intelligence & Knowledge Isolation) happened between 2026-07-30 and 2026-08-07 that this snapshot predates entirely.

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
