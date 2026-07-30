# HANDOVER.md

_Written 2026-07-30 as part of a git-prep/handover pass. This describes exactly what is in the working tree at the moment of writing — cross-check against `git log`/`git status` before trusting it if time has passed since._

## Summary

This handover covers a large body of accumulated work — spanning an embedding-dimension production-incident fix, a Business Action hard-delete rework, the Generic Integration Runtime (Integration Schema Studio + Integration Contract layer + ERP Conversation Tester), the Conversation Form Generator/Strategy Engine, a UAT/Regression Suite, and — most recently — a Semantic API Analysis Engine with a live Review & Edit UI and TRANSFORM promoted to a first-class runtime operation type. **None of it was committed to git before this pass.** This document, plus `PROJECT_STATE.md` and `CLAUDE_CONTEXT.md`, exist so another AI assistant (or human) can pick this up without re-deriving any of it from the diff alone.

## Completed Work

- **Embedding pipeline incident fix**: config-precedence bug (`OPENAI_EMBED_MODEL` legacy alias silently overriding the canonical `OPENAI_EMBEDDING_MODEL`) causing a `text-embedding-3-small` (1536-dim) vector into a `VECTOR(3072)` column. Fixed with canonical config + a fail-fast `validate_embedding_configuration()` pre-flight check.
- **Business Action hard-delete rework** — see `docs/adr/0001-business-action-hard-delete.md`.
- **Generic Integration Runtime** — `services/erp_test_harness.py` (product-agnostic conversation/test runtime, migration 031 for `erp_test_cases`), `services/integration_schema_service.py` (Integration Schema Studio, migration 033), `services/integration_contract_service.py` (canonical contract layer).
- **Conversation Form Generator + Conversation Strategy Engine + Hybrid Playground Router**.
- **UAT / Regression Test Suite** (`services/uat_runner.py`, `scripts/run_uat_suite.py`, `uat_dashboard.html`) with persisted run history under `uat_runs/` (gitignored — generated data, not source).
- **Semantic API Analysis Engine + Review & Edit UI** — see `docs/adr/0002-semantic-engine-and-transform-operation.md`. Includes: removal of the old hardcoded intent taxonomy, TRANSFORM promoted to a first-class runtime operation, a live Semantic Analysis panel in the AI-Guided Setup Summary Card with Detected/Override/Effective controls, dynamic (never hardcoded twice) operation-vocabulary dropdowns.
- **3 real draft Business Actions onboarded** from real API documentation (`search_data_shipment_list`, `get_url_product_detail`, `send_line_noti_cs`) — all Draft/Disabled, never executed/published.
- **Git-prep cleanup** (this pass): removed 4 one-off verification scripts and 2 one-off data-seeding scripts from `scripts/` (kept only `run_uat_suite.py`, the genuine reusable CLI tool), removed generated log files, reverted an unused/undocumented `.claude/launch.json` entry, gitignored `uat_runs/*.json`, and — **found and deleted a real secret file (`gusher_secret.txt`, containing live Supabase URL/keys) that was sitting untracked and NOT gitignored** before it could ever be committed.

## Changed Files

Run `git status --short` for the authoritative list at any given moment; as of this writing:

**Modified (16):** `.gitignore`, `admin/routes.py`, `admin/templates/{base,business_actions,preview,settings,sync_activity}.html`, `config.py`, `services/{ai_auto_setup_service,business_action_registry,developer_mode,embedding_service}.py`, `tests/{test_ai_auto_setup_routes,test_ai_auto_setup_service,test_ai_intelligence_pipeline,test_developer_mode}.py`.

**New (production code, 10):** `services/{conversation_analytics_store,conversation_form_generator,conversation_strategy_engine,erp_test_harness,hybrid_playground_router,integration_contract_service,integration_schema_service,semantic_api_analysis_engine,uat_runner,uat_test_data}.py`.

**New (templates, 6):** `admin/templates/{conversation_analytics_dashboard,erp_api_explorer,erp_conversation_tester,erp_test_history,integration_schema_studio,uat_dashboard}.html`.

**New (migrations, 4):** `migrations/{031_erp_test_cases,032_field_metadata,033_integration_action_schemas,034_business_action_audit_log}.sql`.

**New (tests, 12):** `tests/test_{business_action_hard_delete,conversation_analytics_store,conversation_form_generator,conversation_strategy_engine,embedding_service,erp_test_harness,integration_contract_service,integration_schema_service,run_erp_test_strategy_wiring,semantic_api_analysis_engine,semantic_review_edit_ui,uat_runner}.py`.

**New (tooling):** `scripts/run_uat_suite.py`.

**New (docs, this handover pass):** `PROJECT_STATE.md`, `HANDOVER.md`, `CLAUDE_CONTEXT.md`, `docs/adr/0001-business-action-hard-delete.md`, `docs/adr/0002-semantic-engine-and-transform-operation.md`. **Updated:** `docs/{ARCHITECTURE,CURRENT_STATUS,NEXT_STEPS,API,DATABASE}.md`.

## Database Changes

- Migration 031 (`erp_test_cases`) — additive, no existing table altered.
- Migration 032 (`field_metadata`) — additive columns for the Conversation Form Generator's per-field display metadata.
- Migration 033 (`integration_action_schemas`) — new, additive-only table for Integration Schema Studio's versioned schema; never touches `business_actions*`.
- Migration 034 (`business_action_audit_log`) — new, additive-only table; **already applied to the live database by the project owner directly**, verified afterward (table/RLS/policies/indexes exist, a real create+delete cycle produced exactly one correct audit record).
- **`business_actions` deletion semantics changed** (not a migration — a code/behavior change): from soft-delete (`deleted_at`) to permanent `DELETE`. See ADR 0001. No column was dropped; `deleted_at` simply stopped being set by new code (any pre-existing soft-deleted rows from before this change remain in the DB with `deleted_at` set, surfaced via the "Soft Deleted (legacy)" duplicate-dialog branch).

## Breaking Changes

- `DELETE /admin/api/business-actions/{id}` no longer soft-deletes — it is irreversible. The route also no longer accepts a `?force=` query param at all (removed, not just gated) — protected-fixture bypass is Python-only by design.
- `classify_intent()`'s `intent_id` string shape changed for some fixtures (e.g. `tracking_lookup`/`tracking_command` instead of a bare `tracking`) as an intentional side effect of removing the hardcoded taxonomy — spot-check any other consumer relying on the exact old strings (none found as of this writing).
- `erp_test_harness.OPERATION_TYPES` grew from 13 to 14 values (added `TRANSFORM`, appended at the end) — any hardcoded copy of this list elsewhere would need updating; the two known UI copies (Integration Schema Studio's dropdown, the Review & Edit panel) were converted to load dynamically instead of being hardcoded a second time.

## Known Issues

- `tests/test_ai_auto_setup_registry.py::TestDecisionEngineEitherOrderConversation::test_executor_blocks_safely_when_secret_missing` — one known, pre-existing, unrelated failure. Do not treat as a regression from this work.
- See `PROJECT_STATE.md` Known Limitations / Technical Debt for the rest (the orphaned old Review & Edit flow, missing Field Roles override UI, unconfirmed Decision Engine wiring).

## Testing Status

Full suite: **1593 passed, 1 known pre-existing failure**, run via `python -m pytest -q`. No new regressions introduced across any sprint in this body of work (verified after every meaningful change, not just at the end).

## Open Tasks

1. **Resume the paused manual `GetDataCustomer` onboarding walkthrough** exactly where the user left off — this is a live, step-by-step, wait-for-the-user flow, not something to run unattended.
2. Complete/verify the remaining handover workflow steps (git review → commit → wait for approval → push → release summary).
3. See `docs/NEXT_STEPS.md` for the full prioritized backlog.

## Recommended Next Sprint

Per `docs/NEXT_STEPS.md`: P1 items first (Action Executor pre-flight fix, Decision Engine wiring confirmation), then finish manual onboarding verification for the remaining 3 draft actions, add the Field Roles override UI, and schedule the orphaned-code removal as its own dedicated, carefully-tested sprint (not a side effect of any other work).

## Important Notes

- **Do not push without explicit user approval** — this was an explicit constraint on every sprint that produced this diff, and remains one for this handover itself.
- **The dev server (`admin.routes:app` via `.claude/launch.json`'s `shipify-admin` profile) runs with `reload=False`** — any Python code change requires a full stop+restart before it's reflected live; this has caused real bugs in earlier sprints when skipped.
- **Never commit anything matching `gusher_secret.txt`'s shape again** — it is not currently gitignored by name (only `.env*` patterns are); if a similarly-named credential-dump file reappears, delete it before staging, don't just leave it untracked.
- `uat_runs/*.json` is real, useful run-history data generated by the app itself — gitignored, but intentionally left on disk, not deleted.
