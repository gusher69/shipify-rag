# Next Steps

Prioritized backlog. Follow the P0–P3 classification convention from `CLAUDE.md`'s Refactoring Policy when adding to this list — don't let P2/P3 ideas interrupt current delivery.

## P0 — Critical

None outstanding at the time of writing. If a security/data-corruption/production issue is found, it goes here first.

(Resolved 2026-07-30, do not re-flag: a `gusher_secret.txt` file containing real Supabase URL/keys was found sitting untracked in the working directory during a git-prep pass and was deleted before any commit — it was never gitignored, so a future `git add -A` would have leaked it. If a similarly-named file reappears, treat it as a P0 and never commit it.)

## P1 — Required

### 1. Fix Action Executor's pre-flight secret validation
- **Files**: `services/action_executor.py`, verified by `tests/test_ai_auto_setup_registry.py::test_executor_blocks_safely_when_secret_missing`
- **Problem**: when a required `secret_configuration`/`credential_store` value is missing, the executor makes a real HTTP call instead of blocking locally, and the resulting error doesn't name the missing secret.
- **Acceptance criteria**: the failing test passes; the executor returns a blocked/error result *without* making a network call when a required secret cannot be resolved; the error message names the missing parameter.

### 2. Confirm Decision Engine's production wiring
- **Files**: `services/decision_engine.py`, `line_bot/webhook.py`
- **Problem**: unclear from the codebase alone whether `DecisionEngine` is the actual, sole router for live customer messages, or whether older routing logic in `line_bot/webhook.py` still runs in parallel/instead.
- **Acceptance criteria**: a short written confirmation (or a fix) of exactly which code path decides ERP-vs-RAG-vs-both for a real incoming LINE message, added to `docs/ARCHITECTURE.md`.

## P2 — Improvement

### 3. Complete manual ERP onboarding verification for the remaining draft actions
- **Status**: in progress. `GetDataCustomer` onboarding is being manually walked through step-by-step (import → AI-Guided Setup analysis → Semantic Analysis review → auth/search-field verification → validation-group check → Draft/Disabled save → secret configuration → non-destructive API test → conversation-level test) as of this handover. `SearchDataShipmentList`, `GetUrlProductDetail`, and `SendLineNotiCS` were onboarded earlier as drafts but have not been through this same manual verification pass.
- **Acceptance criteria**: all 4 actions manually verified end-to-end (Semantic Analysis correct, secrets configured via Credential Store, at least one non-destructive Test API call succeeds, at least one conversation-level test succeeds) — still saved as Draft/Disabled, never published/enabled as part of this verification.

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
