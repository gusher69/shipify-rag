# Golden Test Harness

Permanent, version-controlled Golden Test Suite for the Shipify AI Platform.
Built 2026-08-16 to replace the original `golden_suite.py` (never committed
to git, deleted after use -- see the Golden Suite Independent Audit report
for the full finding). This directory is now the single source of truth.

## Layout

| File | Role |
|---|---|
| `golden_cases.json` | **Test Definition** -- 50 canonical cases (`GOLDEN-001`..`GOLDEN-050`) plus one supplementary case (`GOLDEN-038B-HANDOFF-DUPLICATE-PROTECTION`, added per audit Phase 6). Each case: `golden_id`, `category`, `description`, `turns`, `expected_route`, `expected_action`, `expected_behavior`, `assertions`. Carries its own `dataset_version`. |
| `golden_assertions.py` | **Assertion Engine** -- pure, stateless checks (`route_equals`, `action_equals`, `no_raw_json`, ...) over a recorded `CaseResult`. No network/DB access, no `golden_id`-specific branches. |
| `golden_runner.py` | **Runner** -- drives each case's turns through the real `/admin/api/hybrid-playground/ask` (Auto mode == the same Decision Engine path the LINE webhook uses), evaluates assertions, persists one immutable row per `(run_id, golden_id)` into `golden_test_results` (migration 041). Never touches `golden_test_registry` (migration 040) -- that table is untouched historical evidence from the audited run. |

## What this harness will never do

- Change `expected_route` / `expected_action` / `expected_behavior` to make a case pass.
- Read or write `golden_test_registry` (the original 50-case audit evidence).
- Overwrite a previous run's results (`UNIQUE(run_id, golden_id)` in `golden_test_results` makes this a hard DB error, not a silent upsert).
- Touch `manual_review_status` / `manual_review_notes` / `reviewed_at` anywhere -- those belong to a human, not this runner.
- Retry a case and hide the first attempt -- one attempt per case, always.
- Convert an `ERROR` into `PASS`, or skip a case without reporting `SKIPPED`.
- Call `services/human_handoff_service.py::send_handoff_notification` (the only function capable of a real `SendLineNotiCS`/NOTIFY send) -- the runner statically verifies this call is absent from `admin/routes.py` before sending a single request, and aborts if it's ever added.

## Running a Golden Suite

```bash
python -m tests.golden.golden_runner \
  --base-url https://<deployed-admin-host> \
  --server-git-sha <sha-confirmed-via-ssh> \
  --run-notes "baseline run after harness rebuild"
```

Or against a local dev server (`admin.preview_server:app` / `admin.routes:app`):

```bash
python -m tests.golden.golden_runner --base-url http://localhost:8010
```

Every run gets a fresh `run_id` and fresh synthetic Playground users
(`playground:GOLDENRUN-<run_id>-<golden_id>`), so re-running the suite never
mutates a previous run's `ai_sessions` transcripts or `golden_test_results`
rows -- each run's evidence stays intact and independently reviewable
(Admin UI: **AI -> Golden Test Runs**, `/admin/golden`).

## Result values

Every case resolves to exactly one of `PASS | FAIL | PENDING | ERROR | SKIPPED`,
and `TOTAL = PASS + FAIL + PENDING + ERROR + SKIPPED` always holds (asserted
in the runner itself). `PENDING` is reserved for cases whose
`expected_route` is intentionally `null` (genuinely ambiguous by design,
e.g. `GOLDEN-030`/`GOLDEN-033`/`GOLDEN-034`/`GOLDEN-050`) -- those can never
auto-`PASS`, only fail a safety assertion (`FAIL`) or await human review
(`PENDING`).

## Adding a new Golden case

Add an entry to `golden_cases.json`'s `cases` array (bump `dataset_version`
if the change is anything beyond adding a brand-new case), using only
assertion `type`s already implemented in `golden_assertions.py`. Do not add
a `golden_id`-specific branch anywhere in `golden_runner.py` or
`golden_assertions.py` -- if a case needs a new kind of check, add a new
generic assertion `type` that any case could use.
