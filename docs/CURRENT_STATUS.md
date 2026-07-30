# Current Status

_Last reviewed: this document reflects the state at the time of git-upload preparation. Update it whenever a feature area materially changes — this is meant to be the single source of truth over the many legacy root-level `.md` files, which describe earlier snapshots of specific features._

## Done

- **RAG Knowledge Base**: ingestion (PDF/Word/Excel/Markdown), chunking, OpenAI embedding (3072-dim pgvector), hybrid retrieval (vector + keyword + heading weighting), query understanding/rewrite, spell correction, confidence scoring, Google Drive nightly sync.
- **Business Action Center**: full CRUD registry, parameter groups (`AT_LEAST_ONE` rules), execution config, response mapping, tags, examples, validation rules, duplicate-key-safe save flow, export/import, embeddings for similarity/duplicate detection.
- **AI Auto Setup / Smart Capability Setup**: paste a cURL/Postman/API doc → AI proposes a full Business Action. Includes:
  - Local (pre-LLM), semantic secret detection across header/body-form/JSON/query-string/basic-auth shapes, with a Credential Store save path (encrypted, masked previews only, never re-typed).
  - A "Review & Edit" page with inline editing of every important field (ERP Name, Capability, Business Description, Search Fields, Example Questions, Response Mapping — rename/hide/show), a sticky action bar, and English-language helper text throughout the Advanced Editor.
  - AI Explainability: "Why?" panels for classification/routing/confidence/authentication/keywords/response-mapping decisions; a Parse Summary ("Detected API Structure"); per-section Detection Confidence; multi-candidate Capability Detection; a Review Score (Analysis Quality / Configuration Completeness / Security / Business Readiness).
  - A deterministic **Semantic Business Layer** on top of the above: canonical 4-level field classification (Technical → Business Label → Canonical Name → Semantic Type), an Intent Model (Capability vs. normalized `entity.lookup` intents with confidence), Entity Detection, a Knowledge Recommendation Engine, a simulated Action Preview, a Semantic Score, and an exportable JSON metadata blob.
  - **Semantic API Analysis Engine** (`services/semantic_api_analysis_engine.py`, 2026-07-29/30) — a second, fully deterministic (no LLM call) classification pass: endpoint intent, multi-role field classification, validation groups, operation safety, response-shape analysis, severity-tagged recommendations, and a Detected/Override/Effective precedence model (`apply_overrides()`). Now genuinely wired into a live consumer: the **Semantic Analysis Review & Edit panel** in the AI-Guided Setup Summary Card. See `docs/ARCHITECTURE.md` and `docs/adr/0002-semantic-engine-and-transform-operation.md`.
- **Generic Integration Runtime** (`services/erp_test_harness.py`, `services/integration_schema_service.py`, `services/integration_contract_service.py`) — product-agnostic test/execution runtime with a 3-layer effective-schema resolver and a canonical, cached Integration Contract every downstream consumer reads. `OPERATION_TYPES` now includes `TRANSFORM` as a first-class value (not aliased to `CALCULATION`).
- **Business Action deletion is now a permanent hard delete** (2026-07-29 — see `docs/adr/0001-business-action-hard-delete.md`), with tiered confirmation, protected-fixture gating, and an immutable audit log (`business_action_audit_log`).
- **Credential Store**: Fernet-encrypted, multi-tenant, audit-logged secret storage, fully separate from the legacy per-variable `secret_configuration` mechanism.
- **LINE OA Webhook**: intent/sentiment/language detection, tone-adjusted replies, Smart Handoff via LINE Notify.
- **User Profiles**: cold/warm/hot segmentation.
- **Admin Web UI**: Knowledge Base, ERP Integration, AI Playground, Prompt Studio, AI Policies, AI Evaluation/Benchmark, Production Validation, File Library, Sync Activity — all present and routed.
- **Test suite**: ~1590 `pytest` tests (grew from the ~85-file unittest baseline as many sprints landed) covering nearly every service/rag module; run via `python -m pytest -q`. One known pre-existing failure — see Known Issues.

## In Progress / Partially Wired

- **Decision Engine** (`services/decision_engine.py`) exists and is tested in isolation, but its production wiring into the live LINE message path vs. the historical routing logic should be double-checked before relying on it as *the* router — verify against `line_bot/webhook.py` before assuming it's the sole decision point.
- **Semantic API Analysis Engine's field-role/entity-mapping overrides** exist on the backend (`apply_overrides()`'s `field_overrides`) but have no dedicated UI control yet in the Review & Edit panel — only Endpoint Intent, Runtime Operation Type, Confirmation/Audit Required, and Validation Groups have override controls today.
- **Response Mapping field-level auto-generation** (Part 8 of the "generated configuration" sprint) only activates when a *real* Test API response is available — it does not (and should not) guess field structure from documentation alone.
- **A large body of work (ERP Test Harness, Integration Schema/Contract layers, Conversation Form Generator/Strategy Engine, UAT Suite, the hard-delete rework, the Semantic API Analysis Engine) exists only in the working tree, never committed to git** — see `HANDOVER.md` for the exact scope. Committing/pushing this is the very next task, gated on this documentation pass.

## Not Done

- No CI pipeline (`.github/workflows` or equivalent) — tests are run manually.
- No automated database backup/restore tooling (see `docs/DATABASE.md` → Backup & Restore) — relies on Supabase's own dashboard backups or manual `pg_dump`.
- No containerization existed prior to this git-prep pass; a `Dockerfile`/`docker-compose.yml` have now been added but are **not yet validated against a real deployment**.
- No channel other than LINE.
- No visual/no-code workflow builder (explicitly a P3 Future Enhancement per `CLAUDE.md`).

## Known Issues

- **`tests/test_ai_auto_setup_registry.py::TestDecisionEngineEitherOrderConversation::test_executor_blocks_safely_when_secret_missing`** — fails on a clean checkout because it exercises a real network call path (the Action Executor should block locally when a required secret is missing, but instead makes a live HTTP request and receives a real 400 response with a generic error message that doesn't mention the missing secret by name). This is a pre-existing gap in the Action Executor's pre-flight validation, not something introduced by recent sprints — flagged but intentionally not fixed under prior sprints' "do not modify Action Executor" constraints. See Next Steps.
- Several legacy root-level docs (`AI_AUTO_SETUP.md`, `SMART_CAPABILITY_SETUP.md`, etc.) describe an earlier version of the AI Auto Setup feature and are now superseded by the behavior described above — treat them as historical context, not current spec.
- `README.md`/`CLAUDE.md` prior to this git-prep pass referenced Qdrant as the vector DB and `uvicorn line_bot.webhook:app` as the primary entry point — both were stale; the actual vector DB is Supabase pgvector and the primary day-to-day entry point is `admin.routes:app`. Corrected in this pass.

## Technical Debt

- `admin/routes.py` is a single ~4700-line file with ~135 routes — a candidate for splitting into per-feature routers (P2, do not do this reactively — see the Refactoring Policy in `CLAUDE.md`).
- No formal migration-tracking table — there's no automated way to know which numbered migrations have been applied to a given Supabase project; applying is currently a manual, ordered process.
- `.env.example` had drifted from what `config.py` actually reads (e.g. `SUPABASE_KEY` vs. the real `SUPABASE_SERVICE_KEY`) — corrected in this git-prep pass; keep this file in sync whenever a new env var is added to `config.py`.
- `admin/templates/business_actions.html` contains a whole orphaned OLD 3-stage AI Auto Setup flow (`baAiRenderReview()`/`#ba-ai-review`, `baAiCollectReviewEdits()`, `baAiOpenAdvancedFromReview()`, `baAiGoToTestStage()`, `baAiSave()`, `baAiTestConnection()` and related helpers) — superseded by the current Summary-Card-based flow (`baAiRenderSummaryCard()`/`#ba-ai-summary-card`) in an earlier sprint, but never removed. Confirmed genuinely unreachable (no `onclick` anywhere calls into it) during the 2026-07-30 Review & Edit UI sprint. Left in place rather than risk a mass deletion without exhaustive per-function call-site re-verification under time pressure — a dedicated cleanup sprint should remove it.

## Mock / Test Data

- `tools/seed_*.py` scripts create example/demo Business Actions and a benchmark dataset — not real customer data, safe to run against any environment.
- `tests/uat_questions.csv`, `tests/smoke_test_cases.md` — manual QA fixtures, not production data.
- Root-level `*.xlsx` test fixtures (`delete-cascade-test.xlsx`, etc.) and `*_b64.txt`/`preview_id*.txt` scratch files are manual-testing artifacts from prior sessions — excluded from git via `.gitignore`, left on disk untouched by this pass (not deleted, per instructions not to remove anything that might still be in use).
- `knowledge/` contains real, previously-ingested documents (including at least one customer-adjacent PDF/xlsx) — excluded from git entirely; only `knowledge/.gitkeep` is tracked so the folder exists after a fresh clone.
