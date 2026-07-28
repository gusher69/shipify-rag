# Next Steps

Prioritized backlog. Follow the P0–P3 classification convention from `CLAUDE.md`'s Refactoring Policy when adding to this list — don't let P2/P3 ideas interrupt current delivery.

## P0 — Critical

None outstanding at the time of writing. If a security/data-corruption/production issue is found, it goes here first.

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

### 3. Wire the Semantic Business Layer's exportable metadata into a real consumer
- **Files**: `admin/templates/business_actions.html` (`baAiBuildSemanticLayer`), target consumer TBD (Playground first, most likely `services/playground_orchestrator.py` or `services/prompt_builder.py`)
- **Why P2 not P1**: the metadata is already fully computed and displayed; wiring it in is additive, not blocking anything today.
- **Acceptance criteria**: at least one consumer (start with AI Playground) reads the semantic layer (canonical field names / detected intents / entities) for at least one concrete use (e.g. richer prompt context), with a test proving it's actually used, not just computed.

### 4. Split `admin/routes.py` into per-feature routers
- **Files**: `admin/routes.py` (~4700 lines, ~135 routes)
- **Acceptance criteria**: routes grouped by feature (`admin/routers/business_actions.py`, `admin/routers/credentials.py`, `admin/routers/knowledge.py`, etc.) mounted via `APIRouter`, with zero behavior change (full test suite passes unchanged, no route path changes).

### 5. Formalize a migration-tracking mechanism
- **Files**: `migrations/`
- **Acceptance criteria**: some way (even a simple `schema_migrations` table + a small apply-script) to know which numbered migrations have run against a given Supabase project, so a new environment (or this git-prep effort) doesn't have to manually verify "030 is the highest number, did we get them all."

### 6. Add CI
- **Files**: new `.github/workflows/test.yml` (or equivalent)
- **Acceptance criteria**: on every PR/push, install deps and run `python -m unittest discover -s tests -q`; the one known-failing test (see `docs/CURRENT_STATUS.md`) is either fixed first (preferred, folds into item #1) or explicitly marked/skipped with a comment linking to the tracked issue so CI is green and meaningful.

## P3 — Future Enhancement

### 7. Formal database backup/restore tooling
- Currently relies on Supabase's dashboard backups or ad-hoc `pg_dump`. A scripted, documented backup/restore flow (`tools/backup_db.py` / `tools/restore_db.py`) would remove the manual-only gap noted in `docs/DATABASE.md`.

### 8. Docker-based local development story
- A `Dockerfile`/`docker-compose.yml` now exist (added during git-prep) but only run the app container against the *hosted* Supabase project — there is no local/dockerized Postgres+pgvector option for fully offline development. Only worth building if offline dev becomes a real need (Supabase's free tier is normally sufficient).

### 9. Multi-channel support
- Currently LINE-only. A generic channel adapter interface (mirroring the ERP Adapter pattern already established for Business Actions) would let a future customer plug in WhatsApp/web-chat without customer-specific code — explicitly a Platform-First candidate per `CLAUDE.md`, but only when a real second channel is needed.

### 10. No-code Business Action workflow builder
- Already named as a P3 item in `CLAUDE.md`'s Platform-First principle — do not build until there's a concrete need beyond the current AI Auto Setup wizard.
