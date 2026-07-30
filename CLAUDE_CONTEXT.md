# CLAUDE_CONTEXT.md

_For a future AI assistant picking up this repository cold. Read this before `CLAUDE.md`'s full detail if you need to get oriented fast — this file is deliberately opinionated and terse. Refreshed 2026-07-30._

## Project Goal

Shipify AI Platform: an AI customer-service agent answering customers on LINE OA, combining a RAG knowledge base with live ERP data through a **configuration-driven** integration layer (Business Actions), plus a full Admin Web UI. It is built to be a reusable platform, not a one-off customer build — see "Platform-First" in `CLAUDE.md`.

## Architecture Principles

1. **Onboarding a new API is a configuration exercise, not a code change.** Every ERP/API integration is described as data (`business_actions*` tables) and executed generically by `services/action_executor.py`. If you find yourself writing `if action_key == "..."` or `if entity == "customer"` anywhere in a runtime/classification module, stop — that is the exact anti-pattern this codebase has repeatedly audited and removed (see `docs/adr/0002-...md`'s Context section for the most recent concrete instance).
2. **Deterministic vs. LLM-generated logic is always separably labeled.** An LLM proposes; deterministic Python functions post-process, classify, and validate. Never blur "the AI said this" with "we computed this."
3. **Never let unreviewed AI output auto-save or auto-execute.** Every AI proposal goes through an explicit Review & Edit step; every new Business Action saves as Draft/Disabled until an admin explicitly enables it.
4. **A new classification/derivation system does not replace an old, tested one without a documented reason.** See `docs/adr/0002-semantic-engine-and-transform-operation.md`'s Alternatives Considered — the Semantic API Analysis Engine was added *alongside* `erp_test_harness.py`'s runtime classifier, bridged by one explicit mapping function, specifically to avoid touching the latter's blast radius.

## Coding Rules

- Python 3.13, FastAPI, no ORM (raw SQL migrations in `migrations/`, numbered, never edit an applied one).
- Tests: `pytest` (the historical `unittest`-only convention has been superseded — this repo now has ~1590 pytest tests; run `python -m pytest -q`, not `python -m unittest discover`).
- `config.py` is the only place `os.getenv` is called — everything else imports the resolved constant.
- Admin UI: server-rendered Jinja2 (`admin/templates/*.html`) + inline vanilla JS. No SPA framework, no build step. New UI work follows this same pattern.
- English for code/comments/UI labels; Thai only for genuinely customer-facing conversational text.
- Comments explain *why*, not *what* — this repo's existing comment density is high specifically to preserve non-obvious rationale (a prior incident, a deliberate trade-off, a constraint from an earlier sprint) across long AI-assisted development chains. Match that style.

## Project Conventions

- **`setup_metadata` (JSONB on `business_actions`) is the extension point** for anything additive that doesn't need its own column/table — `routing`, `conversation_behavior`, `operation_type`, `semantic_analysis`, `semantic_overrides` all live there. Prefer this over a new migration for genuinely additive, non-relational data.
- **Detected → Admin Override → Effective** is the established 3-layer precedence pattern for anything an AI/deterministic engine derives but an admin might need to correct (`integration_schema_service.py`'s schema resolver, `semantic_api_analysis_engine.py`'s `apply_overrides()`). If you build a new derived-value feature, reuse this pattern rather than inventing a 4th precedence system.
- **One-off scripts are disposable.** `scripts/` should contain only genuinely reusable tooling (currently: `run_uat_suite.py`). Verification/one-time-data-creation scripts get deleted once their job is done — do not leave them in the repo "just in case"; a git-prep pass on 2026-07-30 removed 6 of them for exactly this reason.
- **Never commit secrets.** `.gitignore` covers `.env*` patterns but not arbitrary filenames — a real secret file (`gusher_secret.txt`) was found untracked and un-gitignored during the 2026-07-30 handover and deleted before it could be committed. Scan for anything secret-shaped before staging, don't assume `.gitignore` catches it.

## Important Design Decisions

See `docs/adr/` for the full write-ups. Summary:
- **ADR 0001**: Business Action deletion is a permanent hard delete (not soft-delete) with an immutable audit log and protected-fixture gating.
- **ADR 0002**: The Semantic API Analysis Engine is a separate, deterministic, setup-time-only classifier from the runtime's `erp_test_harness.py` classifier — bridged, not merged. TRANSFORM is a first-class runtime operation type, never aliased to CALCULATION.

## Current Canonical Architecture

```
line_bot.webhook:app  ──┐
                          ├── shares services/, rag/, ingestion/
admin.routes:app  ───────┘

AI-Guided ERP Setup:
  paste API def → analyze (LLM + deterministic post-processing +
  semantic_api_analysis_engine.analyze_endpoint()) → Summary Card
  (Semantic Analysis panel, Detected/Override/Effective) → save
  (Draft/Disabled) → setup_metadata.{operation_type, semantic_analysis,
  semantic_overrides} persisted.

Runtime read path for an already-saved action:
  erp_test_harness.infer_operation_type_with_evidence() reads
  setup_metadata.operation_type (tier 2, pre-existing) →
  integration_schema_service.resolve_effective_integration_schema() →
  integration_contract_service.describe_integration() (canonical,
  cached) → every downstream consumer (ERP Conversation Tester,
  Playground, future Multi-ERP orchestration) reads the contract,
  never re-derives.
```

## Do

- Read `PROJECT_STATE.md` and `HANDOVER.md` first if either exists and looks fresher than the last commit you can see — a large amount of work in this repo has historically accumulated uncommitted for many sessions at a time.
- Run the full test suite (`python -m pytest -q`) before AND after any change; the baseline is 1593 passed / 1 known pre-existing failure (`test_executor_blocks_safely_when_secret_missing`) — treat any OTHER failure as a real regression to fix before proceeding.
- Restart the dev server after any Python change (`reload=False` in `.claude/launch.json`) before live-testing in the browser — a stale server has caused real, hard-to-diagnose bugs in this project before.
- When adding a UI dropdown/list whose values come from a canonical backend enum, load it dynamically (`fetch()` a route) rather than hardcoding a second copy — this exact anti-pattern was found and fixed twice in one sprint (TRANSFORM missing from two separately-hardcoded dropdown lists).
- Verify a UI change live in the browser (this project's dev server + browser-preview tooling) before claiming it works — unit tests do not exercise the inline JS. Check that the DOM element you're targeting is actually reachable from a real button click, not just present somewhere in the template — an entire sprint's work was initially wired into a dead, unreachable function before this was caught.

## Don't

- Don't add a per-domain/per-entity hardcoded branch (`if entity == "customer"`, a keyword regex table keyed to specific words) to any classification/runtime module — this has been audited and removed multiple times already; it directly violates the platform's core value proposition.
- Don't touch `services/erp_test_harness.py`'s existing classifier vocabulary/logic to add a new concept — bridge into it via `setup_metadata.operation_type`/a mapping function instead (see ADR 0002).
- Don't silently convert/backfill existing data based on a new classification (e.g. reclassifying old `CALCULATION` rows to `TRANSFORM`) without checking first whether any real rows are actually affected — verify against the live DB, don't assume.
- Don't leave a one-off verification/seed script in `scripts/` after its job is done.
- Don't commit without running a full untracked-file audit first — `git status --short` and actually look at what's untracked, don't just `git add -A`.
- Don't push without explicit user approval, ever, even after a successful local commit.

## Current Sprint

Manual, human-in-the-loop verification of ERP API onboarding, starting with `GetDataCustomer` — paused mid-flow for this handover. If you are resuming this, **wait for the user at each step**; this is explicitly not an autonomous task. See `PROJECT_STATE.md`'s Current Sprint section for exact status.

## Priority Modules

`services/semantic_api_analysis_engine.py`, `services/erp_test_harness.py`, `services/business_action_registry.py`, `admin/routes.py` (the AI Auto Setup + Integration Schema/Contract route groups), `admin/templates/business_actions.html` (the live `baAiRenderSummaryCard()` path — NOT the dead `baAiRenderReview()` path, see Known Risks).

## Known Risks

- `admin/templates/business_actions.html` contains a large orphaned old UI flow that looks live (function names, DOM ids) but is never actually reached from any button. Before editing anything in this file, confirm with `grep -n "onclick=\"functionName"` that your target function is actually wired to a visible control, not assuming naming/position implies reachability.
- The dev server dies between sessions/turns in this sandboxed environment more often than a real deployment would — always verify it's actually running (and running fresh, post any code change) before live-testing, rather than assuming a prior `preview_start` is still good.
- A body of substantial, tested, working code can sit uncommitted for a long time in this project's history — don't assume `git log`'s HEAD reflects current reality; always check `git status` first.

## Current Development Philosophy

Ship real, tested, deterministic increments; document the *why* behind every non-obvious decision as an ADR or an inline comment; never let an LLM call substitute for a deterministic guarantee where one is needed (secret redaction, classification determinism, validation); prefer bridging/composing existing tested modules over replacing them; verify claims against the live system (DB queries, live browser checks, real test runs) rather than reasoning from memory or assumption.
