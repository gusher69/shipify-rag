# Next Steps

Prioritized backlog. Follow the P0–P3 classification convention from `CLAUDE.md`'s Refactoring Policy when adding to this list — don't let P2/P3 ideas interrupt current delivery.

## P0 — Critical

None outstanding at the time of writing. If a security/data-corruption/production issue is found, it goes here first.

(Resolved 2026-07-30, do not re-flag: a `gusher_secret.txt` file containing real Supabase URL/keys was found sitting untracked in the working directory during a git-prep pass and was deleted before any commit — it was never gitignored, so a future `git add -A` would have leaked it. If a similarly-named file reappears, treat it as a P0 and never commit it.)

### A. Replace legacy webhook routing with Decision Engine (2026-08-02 Decision Engine audit)
- **Status**: ✅ RESOLVED (confirmed live as of 2026-08-10 deployment-freeze pass). `line_bot/webhook.py::_handle_message_via_decision_engine` routes real LINE traffic through `DecisionEngine.decide()`; `config.DECISION_ENGINE_LIVE_ROUTING` defaults to `true`. The legacy `intent.classify()` path (`_handle_message_legacy`) still exists as an isolated fallback (flip the env var to `false` to use it) but is not the default. Hybrid routing and the generic confirmation gate (see item C below) are both live on this same path.
- **Files**: `line_bot/webhook.py`, `services/decision_engine.py`

## P1 — Required

### 1. Fix Action Executor's pre-flight secret validation
- **Files**: `services/action_executor.py`, verified by `tests/test_ai_auto_setup_registry.py::test_executor_blocks_safely_when_secret_missing`
- **Problem**: when a required `secret_configuration`/`credential_store` value is missing, the executor makes a real HTTP call instead of blocking locally, and the resulting error doesn't name the missing secret.
- **Acceptance criteria**: the failing test passes; the executor returns a blocked/error result *without* making a network call when a required secret cannot be resolved; the error message names the missing parameter.

### 2. Confirm Decision Engine's production wiring — RESOLVED, see P0 item A
- Kept here only as a pointer: P0 item A is now fully resolved (confirmed wired and live as of 2026-08-10) — nothing further to do here.

### B. Implement Hybrid routing inside Decision Engine
- **Status**: ✅ RESOLVED (2026-08-02 Production Integration Sprint, confirmed live 2026-08-10). `DecisionEngine._handle_hybrid_turn` segments a question into an ERP sub-question and a RAG sub-question (`services/hybrid_question_classifier.py`), runs each exactly once, and combines them via `services/hybrid_runtime_service.py::synthesize_hybrid_answer` (re-exported from `hybrid_playground_router.py`) — the same explicit, structured, non-LLM synthesis the AI Playground's Hybrid mode uses. Regression-tested (`tests/test_decision_engine.py::TestHybridRouting`) and re-verified live this pass ("ลูกค้า SP1014 มีคูปองอะไร และคูปองใช้งานอย่างไร" → one real ERP call + one real RAG call, correctly labeled sections).
- **Files**: `services/decision_engine.py`, `services/hybrid_question_classifier.py`, `services/hybrid_runtime_service.py`

### C. Enforce `confirmation_required` before execution
- **Status**: ✅ RESOLVED (2026-08-10, built for the SendLineNotiCS enablement). `services/decision_engine.py::_requires_confirmation` is a generic, metadata-driven gate — ANY Business Action whose operation type is COMMAND-type (`NOTIFICATION`/`NOTIFY`/`WORKFLOW` today, extensible) is blocked from reaching the Action Executor without an explicit `context={"confirmed": True}` signal for that turn, reusing the same operation-type classification `services/erp_test_harness.py`'s Playground gate already used (one source of truth, not a duplicate rule). `line_bot/webhook.py` + `services/pending_confirmation_service.py` persist the pending state (table `pending_confirmations`, migration 036) across LINE turns, recognize natural confirm/cancel replies, expire after a configurable timeout (`config.PENDING_CONFIRMATION_TIMEOUT_SECONDS`, default 300s), and are scoped by tenant/channel/conversation so one user can never confirm another's pending action. Regression: `tests/test_decision_engine.py::TestConfirmationGateForCommandActions`, `tests/test_pending_confirmation_service.py`, `tests/test_webhook.py::TestLineConfirmationFlow` (scenarios A–G).
- **Files**: `services/decision_engine.py`, `services/pending_confirmation_service.py`, `line_bot/webhook.py`, `migrations/036_pending_confirmations.sql`

### D. Implement confidence-threshold escalation in Decision Engine
- **Files**: `services/decision_engine.py`, `config.py` (`CONFIDENCE_THRESHOLD`)
- **Problem**: CLAUDE.md's stated business rule ("confidence below `CONFIDENCE_THRESHOLD` → Smart Handoff") has no implementation in `decision_engine.py` — `config.CONFIDENCE_THRESHOLD` is never referenced there. Today `decide()` only escalates on an explicit human-request phrase, a refusal phrase, or max-retry-exceeded. (Partially unblocked 2026-08-02: the RAG Executor's confidence score is now surfaced into `developer_trace["confidence"]` instead of being silently discarded — see `services/decision_engine.py::_execute_selected_action`. Actually enforcing a threshold-based escalation is still open.)
- **Acceptance criteria**: a low-confidence result (below `CONFIDENCE_THRESHOLD`) routes to `_route_human_handoff` the same way the existing triggers do; a test proves it.

### F. Onboard GetUrlProductDetail — RESOLVED (2026-08-09)
- **Status**: ✅ enabled, production-ready. Real parameter name `URL` confirmed via the customer's pasted Postman spec. Real 200 with a live 1688.com URL, honest 400 on an invalid domain.

### G. Onboard SendLineNotiCS — RESOLVED (2026-08-10)
- **Status**: ✅ enabled, `Message` parameter confirmed, generic confirmation gate built (see item C above) — see `PROJECT_STATE.md` Current Sprint for the full detail. **No real notification has been sent** — the first real send is a deliberate, explicitly-confirmed test to be performed after deployment, per standing instruction.

### H. Knowledge Base cleanup — RESOLVED (2026-08-10)
- **Status**: ✅ Applied and verified. 15 unrelated vendor/demo/internal-ops files (Allianz insurance, Dew Dev Ops, CONTRACTORSHOP contract, hosting quotations, raw logs, `requirements.txt`, `ZWIZ.AI_SME 2023`) set `is_active: false` on both `knowledge_files` and `knowledge_chunks` — disabled, not hard-deleted. Only `AI Knowledge Master.xlsx`/`AI Knowledge Master (1).xlsx` remain active. Verified with the original RAG query plus 4 adversarial queries — zero contamination retrievable. See `PROJECT_STATE.md` Current Sprint for full detail.
- **Files**: `knowledge_files`, `knowledge_chunks` (Supabase); local `knowledge/` directory still holds the disabled files' original uploads (`STORAGE_PROVIDER=local` on this dev machine) — harmless since they're excluded from retrieval, but worth cleaning up if disk space matters, not urgent.

## P2 — Improvement

### E. Improve example-based action scoring
- **Files**: `services/decision_engine.py` (`_keyword_score`, `search_candidate_actions`), `services/business_action_registry.py` (`get_examples`)
- **Problem**: `_keyword_score()` reads `action.get("_examples_text")`, but nothing ever populates it — real example questions live in the `business_action_examples` table (via `registry.get_examples()`) and are never attached to a candidate before scoring. The "example overlap" portion of action-selection scoring is permanently dead code.
- **Why P2 not P1**: wiring it in naively adds one DB call per candidate per turn to the hot `decide()` path — a real latency/scaling tradeoff (Platform-First: must not regress at "100 customers" scale), so it needs a batched/cached fetch strategy, not a blind one-line fix.
- **Acceptance criteria**: example-question overlap actually contributes to action selection scoring, with no N+1-per-turn query pattern introduced (e.g. batch-fetch once per candidate set, or cache on the registry side).

### 3. GetDataCustomer ERP Integration — FROZEN pending customer-side fix (2026-08-02)
- **Status**: ⏸️ Frozen. Not a Shipify platform defect — confirmed external dependency issue.
- **Done**:
  - ✅ AI Auto Setup completed
  - ✅ Business Action Center completed
  - ✅ Credential Store completed (self-service UI built this same day — see `admin/templates/credential_store.html`)
  - ✅ Semantic API Analysis Engine completed
  - ✅ Decision Engine can invoke ERP
  - ✅ HTTP Request construction verified
  - ✅ Credential resolution verified
- **Pending** (blocked on the customer, not on us):
  - Customer fixes their FastTrade ERP API — multiple identifiers (e.g. `CustCode=C00001`, sourced directly from the customer) return `{"status":"error","message":"ไม่พบข้อมูล"}` ("data not found"), reproduced identically via our system AND an independent raw `curl` bypassing our codebase entirely — same request, same real secret, same result. This rules out a Shipify-side bug.
  - Customer provides a confirmed-valid Success Response sample (needed to complete Response Mapping — currently only maps the whole `$.data` blob to one label, not separate name/wallet/coupon fields, because AI Auto Setup was never given a real example response).
  - Response Mapping (blocked on the above).
  - End-to-End ERP Verification — Scenarios A–F, Decision Engine auto-selection, AI Playground ERP mode, AI Playground Hybrid mode (all blocked on the above).
- **Do not** spend further time debugging the ERP endpoint itself until the customer supplies working test data or fixes their API. `SearchDataShipmentList`, `GetUrlProductDetail`, and `SendLineNotiCS` were onboarded earlier as drafts and have not been through this same verification pass — same "needs real customer test data" blocker likely applies.
- **Acceptance criteria (once unblocked)**: all 4 actions manually verified end-to-end (Semantic Analysis correct, secrets configured via Credential Store, at least one non-destructive Test API call succeeds against real data, at least one conversation-level test succeeds) — still saved as Draft/Disabled, never published/enabled as part of this verification.

### 4. Add a Field Roles / Entity Mapping override control to the Semantic Analysis panel
- **Files**: `admin/templates/business_actions.html` (`baAiRenderSemanticPanel`), `services/semantic_api_analysis_engine.py` (`apply_overrides()`'s `field_overrides` — already supports this on the backend)
- **Why P2 not P1**: the backend support already exists and is tested; only the UI control is missing.
- **Acceptance criteria**: an admin can override a field's role(s)/business_entity label from the Review & Edit panel, see it reflected in the Effective view, and Reset it back to Detected.

### 5. Remove the orphaned old AI Auto Setup Review & Edit flow
- **Files**: `admin/templates/business_actions.html` (`baAiRenderReview`, `#ba-ai-review`, `baAiCollectReviewEdits`, `baAiOpenAdvancedFromReview`, `baAiGoToTestStage`, `baAiSave`, `baAiTestConnection`, `baAiRenderTestParams`)
- **Why P2 not immediate**: confirmed genuinely unreachable (no live `onclick` calls into it), but removing ~500 lines of intertwined functions safely requires re-verifying every call site individually — deferred rather than risked during the 2026-07-30 documentation/handover pass. See `docs/CURRENT_STATUS.md` Technical Debt.
- **Acceptance criteria**: the dead functions are removed, full test suite + a live manual pass through AI-Guided Setup still work identically, no console errors.

### 6. Wire the Semantic Business Layer's exportable metadata into a real consumer
- **Files**: `admin/templates/business_actions.html` (`baAiBuildSemanticLayer`), target consumer TBD (Playground first, most likely `services/playground_orchestrator.py` or `services/prompt_builder.py`)
- **Why P2 not P1**: the metadata is already fully computed and displayed; wiring it in is additive, not blocking anything today.
- **Acceptance criteria**: at least one consumer (start with AI Playground) reads the semantic layer (canonical field names / detected intents / entities) for at least one concrete use (e.g. richer prompt context), with a test proving it's actually used, not just computed.

### 7. Split `admin/routes.py` into per-feature routers
- **Files**: `admin/routes.py` (~4700 lines, ~135 routes)
- **Acceptance criteria**: routes grouped by feature (`admin/routers/business_actions.py`, `admin/routers/credentials.py`, `admin/routers/knowledge.py`, etc.) mounted via `APIRouter`, with zero behavior change (full test suite passes unchanged, no route path changes).

### 8. Formalize a migration-tracking mechanism
- **Files**: `migrations/` (currently through 034)
- **Acceptance criteria**: some way (even a simple `schema_migrations` table + a small apply-script) to know which numbered migrations have run against a given Supabase project, so a new environment doesn't have to manually verify "034 is the highest number, did we get them all."

### 9. Add CI
- **Files**: new `.github/workflows/test.yml` (or equivalent)
- **Acceptance criteria**: on every PR/push, install deps and run `python -m pytest -q`; the one known-failing test (see `docs/CURRENT_STATUS.md`) is either fixed first (preferred, folds into item #1) or explicitly marked/skipped with a comment linking to the tracked issue so CI is green and meaningful.

## P3 — Future Enhancement

### 10. Formal database backup/restore tooling
- Currently relies on Supabase's dashboard backups or ad-hoc `pg_dump`. A scripted, documented backup/restore flow (`tools/backup_db.py` / `tools/restore_db.py`) would remove the manual-only gap noted in `docs/DATABASE.md`.

### 11. Docker-based local development story
- A `Dockerfile`/`docker-compose.yml` now exist (added during git-prep) but only run the app container against the *hosted* Supabase project — there is no local/dockerized Postgres+pgvector option for fully offline development. Only worth building if offline dev becomes a real need (Supabase's free tier is normally sufficient).

### 12. Multi-channel support
- Currently LINE-only. A generic channel adapter interface (mirroring the ERP Adapter pattern already established for Business Actions) would let a future customer plug in WhatsApp/web-chat without customer-specific code — explicitly a Platform-First candidate per `CLAUDE.md`, but only when a real second channel is needed.

### 13. No-code Business Action workflow builder
- Already named as a P3 item in `CLAUDE.md`'s Platform-First principle — do not build until there's a concrete need beyond the current AI Auto Setup wizard.

### 14. Lead Intelligence module (proposed 2026-08-05, builds on Phase 3 Conversation History + User Profile)
- **Title:** Lead Intelligence — sales/CRM scoring layer on top of Conversation History + User Profile.
- **Reason:** Once Phase 3's Conversation History (`ai_sessions`/`ai_session_messages`/`ai_session_traces`) and User Profile stats (`user_profiles`) exist, every signal a lead-scoring model needs (topics asked, products mentioned, ERP lookups, question frequency, sentiment) is already being captured turn-by-turn — no new data-collection system required.
- **Business value:** Surfaces sales-actionable signals per customer: Interest Score, products of interest, ERP items searched, frequently-asked topics, a Conversion Score, and a Next Best Action suggestion (e.g. "offer a promotion", "have sales call", "send an article") — turns the AI assistant into a passive lead-qualification tool for the sales team, not just a support bot.
- **Estimated complexity:** Medium — a new read-only scoring service (`services/lead_intelligence_service.py`) that aggregates existing `user_profiles`/`ai_sessions` data with a deterministic scoring function (consistent with this repo's convention of keeping AI-generated output and deterministic post-processing separately labeled, per `CLAUDE.md`), plus one new Admin UI view. No new ingestion pipeline, no schema changes to Conversation History itself.
- **Suggested timing:** After Phase 3 (Conversation History + User Profile + Customer Tier) ships and has real production data flowing through it — scoring against empty/thin data isn't useful. Natural "Phase 4."
