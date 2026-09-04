# REGRESSION-GATE-1 — Regression Gate Definition

> **Update (CUSTOMER-LINK-1):** the Product Link Conversion capability
> (`geturlproductdetail`) was implemented on top of this gate. `CUS-P20`
> moved FAIL → PASS (see `CUSTOMER_MASTER_FAILURE_INVENTORY.md`); Customer
> Master is now 48/69. A genuine conflict surfaced during that work
> (a prior test suite claimed the real upstream empirically requires
> CustCode; CUS-P20's customer source says the opposite) was resolved by
> the product owner in favor of the customer source — see `tests/
> test_decision_engine.py::TestUrlConversionActionAndCrossActionIdentifierReuse`'s
> class docstring for the full resolution. `tests/customer_uat/
> known_baseline_case_status.json` was regenerated to reflect this as the
> new reference point for future gate runs.

**Locked checkpoint:** `0702e64216fcc75401920cf3f6780dc9fe179b0f` — manually
smoke-tested by the product owner on REAL LINE (6 journeys, all PASS; see
"Locked REAL-LINE cases" below). This gate exists to make sure nothing that
passed at that checkpoint silently regresses again, and to give every future
business-logic deployment ONE repeatable command to run before deploying.

This is an audit-and-build task: it did **not** change production business
logic (see "Production code changed" at the bottom) — it fixes two genuine
test-infrastructure isolation leaks (see "Test isolation" below), makes the
Customer Master harness non-destructive by default, and adds three
permanent, individually-named regression registries.

## What the gate is (and isn't)

- **Regression Gate** = "did anything that used to pass stop passing?" —
  the deploy/no-deploy signal. Clean gate ⇒ safe to deploy this change.
- **Release Readiness** = "how many of the 69 customer-required cases pass
  right now, exactly?" — a separate, honest measurement. A clean gate does
  **not** imply release readiness; see `CUSTOMER_MASTER_FAILURE_INVENTORY.md`
  for the real number (47/69) and why each of the 22 gaps exists.

Run both together, always, with:

```bash
python -m tests.run_regression_gate
```

(`tests/run_regression_gate.py` — composes existing unittest runners and
`run_baseline.py`; no new test-running framework.)

## The four layers

1. **Protected / focused suites** — every unittest module the locked
   checkpoint depends on (24 modules: decision engine, semantics,
   calculator, charter/TC19, coupon, invoice, ERP-read, authorization, the
   SYSTEM-STATE-EMERGENCY-1 journey, business-action registry, ...). See
   `PROTECTED_SUITES` in `tests/run_regression_gate.py` for the exact list.
2. **Customer Master** — `tests/customer_uat/run_baseline.py`, default
   SCRATCH mode (`tests/customer_uat/.gate_scratch/`, gitignored; the
   tracked `baseline_results.json` / `CUSTOMER_UAT_BASELINE_REPORT.md` are
   only touched by an explicit `--commit-baseline` run), diffed
   case-by-case against `tests/customer_uat/known_baseline_case_status.json`
   (the locked-checkpoint per-case PASS/FAIL snapshot). A case flipping
   PASS→FAIL is a NEW regression and blocks the gate; a case that was
   already failing and stays failing does not (see
   `CUSTOMER_MASTER_FAILURE_INVENTORY.md` for why each pre-existing failure
   is classified the way it is).
3. **Known REAL-LINE Regression** — `tests/test_known_real_line_cases.py`,
   one test per named `RL-*` case (registry below).
4. **Cross-Flow Matrix** — `tests/test_cross_flow_matrix.py`, `CF-01`
   through `CF-14` (registry below).

## Deploy rule (going forward, permanent)

Every future business-logic deployment runs, in order: (1) its own focused
task tests, (2) the Customer Master layer, (3) the Known REAL-LINE suite,
(4) the Cross-Flow Matrix — i.e. `python -m tests.run_regression_gate`. If
any previously-passing protected case becomes FAIL: **BLOCK DEPLOYMENT**,
no exceptions unless the product owner explicitly approves the regression.

## Known REAL-LINE Regression registry

Each case audited against its customer source **before** its assertion was
written (never invented). Full detail and source citations live as
docstrings on each test class in `tests/test_known_real_line_cases.py`.

| ID | Message | Requirement | Status |
|---|---|---|---|
| RL-STATE-01 | ช่วยคำนวณค่าส่ง... (with a pending order-lookup) | Calculator must win over a pending unrelated flow | PASS |
| RL-STATE-02 | ร้านส่งหรือยังคะ (with a pending charter) | Shipment Status must win over a pending charter | PASS |
| RL-COUPON-01 | ผมมีคูปองอะไรบ้าง | Must be PRIVATE ERP (MY_COUPONS); verified FT3182 = no coupons | PASS |
| RL-COUPON-02 | คูปองใช้ยังไงครับ | Must be PUBLIC RAG; must never demand a customer code | PASS |
| RL-INVOICE-01 | ใบกำกับค่าสินค้าออกได้ไหม | Must answer directly; must not ask "สินค้าของลูกค้าเป็นอะไร" | PASS |
| RL-CALC-02 | full calc → road → "ถ้าเป็นเรือล่ะ" → sea → new episode | Retains context across the comparison; no inheritance into a new episode | PASS |
| RL-TC19-01 | charter open → partial slots → remaining slots | Asks only the missing slots; completes/hands off once all 4 are given | PASS |
| RL-AMBIGUOUS-01 | ของผมล่ะ (no referent) | CLARIFICATION, never a private ERP dump | PASS |
| RL-AMBIGUOUS-02 | แล้วอันนี้ล่ะ (no referent) | CLARIFICATION, never a private ERP dump | PASS |
| RL-WAREHOUSE-01 | ไปรับของแถวไหนครับ | PUBLIC info; must not call ERP; must not fabricate a "done" reply | PASS |
| RL-TRACK-TH-01 | ขอแทรคไทยค่ะ | Must never ask for a Chinese tracking number (customer's explicit hard constraint) | PASS — see source audit note in the test file and `CUSTOMER_MASTER_FAILURE_INVENTORY.md` (CUS-S17) |
| RL-PRODUCT-01 | กล่องพลาสติกนำเข้าได้ไหมครับ | Route = PRODUCT_POLICY (no flow theft) | PASS (route) — **KNOWN FAILURE** (answer is still a generic service-ack, not a prohibited-goods verdict; `@expectedFailure`, tracked permanently) |
| RL-CUSTOM-SERVICE-01 | สั่งสกรีนโลโก้เสื้อได้ไหมคะ | Source (Ai.xlsx CSW6) has an approved ack+collect+coordinate process | **KNOWN FAILURE** — no operational-change kind covers custom production yet; `@expectedFailure`, tracked permanently, not invented as PASS |

## Cross-Flow Matrix registry (`tests/test_cross_flow_matrix.py`)

All accumulated-session (a real turn 1 opens the pending flow, a real turn
2 sends the cross-flow message, in the SAME growing history) — never an
isolated one-shot message.

| ID | Pending flow | New message | Winner | Status |
|---|---|---|---|---|
| CF-01 | Shipment status pending | Calculator request | Calculator | PASS |
| CF-02 | Calculator pending | Coupon usage (public) | Coupon usage | PASS |
| CF-03 | Calculator pending | My Coupons (private) | My Coupons | PASS |
| CF-04 | Calculator pending | TC19 (charter) | TC19 | PASS |
| CF-05 | Charter pending | Calculator | Calculator | PASS |
| CF-06 | Charter pending | Coupon usage | Coupon usage | PASS |
| CF-07 | Charter pending | Invoice | Invoice | PASS |
| CF-08 | Charter pending | Shipment Status | Shipment Status | PASS |
| CF-09 | Charter pending | Transit Time | Transit Time | PASS |
| CF-10 | Charter pending | Address Change | Operational Action | PASS |
| CF-11 | Private (shipment) pending | Public Transit Time | Public Transit | PASS |
| CF-12 | Invoice answered | Shipment Status | Shipment Status | PASS |
| CF-13 | Coupon usage answered | Calculator | Calculator | PASS |
| CF-14 | TC19 complete | New Calculator | New episode, no stale charter-slot leakage | PASS |

## Locked REAL-LINE cases (manually verified at `0702e64`, must never silently regress)

Calculator initial request · Calculator SEA continuation (route comparison)
· Seller/store shipment-status wording · Coupon Usage · Invoice · Transit
Time · Referentless ambiguity clarification. Each is exercised by the
protected suites and/or the `RL-*` / `CF-*` registries above.

## Test isolation — audited, fixed, evidenced

Two genuine cross-file test-isolation leaks were found and fixed (test
infrastructure only; no production behaviour changed):

1. **`_CONFIG_CACHE` fake-vs-fake leak** (`services/business_action_
   registry.py`'s process-wide 300s TTL read cache, keyed by a plain
   string with no per-`sb` scoping — a real, correct production feature
   for admin-edited static config, but unsafe across dozens of test files
   sharing one process). Fixed by clearing it at the start of every fresh
   `_FakeSupabase()` fixture's life (`tests/test_business_action_
   registry.py::_FakeSupabase.__init__`). Verified: `tests.test_decision_
   engine` alone went from 3 known failures to `Ran 371 tests ... OK`.
2. **Real-registry singleton leak** (`business_action_registry._instance`,
   the module-level `BusinessActionRegistry` cached for the process when
   `DecisionEngine()` is built with no `sb` — the SYSTEM-STATE-EMERGENCY-1
   methodology of replaying against the REAL, deployed registry rather
   than a hand-seeded approximation). `_resilient_read`'s legitimate
   production fail-open behaviour (an 18s wall-clock read timeout returns
   `.data == []` rather than hanging the LINE webhook) could, under the
   combined-suite network load, cache an EMPTY registry for the rest of a
   ~220–340s run, starving every later real-registry test of its business
   actions (`SAFE_FALLBACK` / `src=None` routing). Reproduced directly: a
   full 825-test combined run had 10 failures before the fix, `Ran 825
   tests ... OK` after. Fixed by `tests/test_business_action_registry.py::
   reset_real_registry()`, called at the top of `setUpClass` in the 7
   affected files (`test_system_state_emergency_1`, `test_calculator_
   regression_2`, `test_customer_action1`, `test_semantic_first_1`,
   `test_customer_calc1`, `test_customer_calc11_conversation_state`,
   `test_customer_rag2_1_charter_slots`) — bounds any transient-timeout
   blast radius to one test class instead of the rest of the suite.
   `_resilient_read`'s production fail-open behaviour is untouched; this
   is the same latent edge case in a long-lived production process (a
   cold-start read timeout would similarly cache empty for 300s there
   too), recorded as a P2 Technical Debt item, not fixed here (a
   production behaviour change needs its own audit/justification, and the
   spec's "do NOT change production behaviour unless strictly necessary to
   make the regression harness truthful" scopes this fix to test infra).

3. **`test_customer_uat_fix1_public_private_boundary.py` — 3 failures,
   re-diagnosed.** Previously assumed (carried over from prior phases) to
   be `_CONFIG_CACHE` pollution. Re-tested in isolation after the fix
   above: still failed alone, proving it was NOT cache pollution. Full
   debug trace showed the actual `decide()` output for
   `"ขอเช็กพัสดุเดียวครับ"` is fully customer-correct (routes WORKFLOW,
   asks for the transport bill, selects `searchdatashipment`, no premature
   ERP call) — the only discrepancy is the internal `turn_intent_coerced`
   trace value now reading `"private_state_inquiry"` (SEM-1, shipped
   2026-09-03, one day AFTER these Fix-1 tests were written 2026-09-02)
   instead of the test's expected `None`. Classified **STALE_EXPECTATION**
   with evidence; fixed via 3 test-only assertion edits (`assertNotEqual
   (turn_intent_coerced, "public_clarification_continuity")` instead of
   `assertIsNone(...)`), keeping `mock_req.assert_not_called()` as the
   real business-fact assertion. No production code changed.

**Full suite verification:** `python -m unittest` over the 24 protected
modules + the two new registries (29 tests) — **`Ran 825 tests ... OK`**
(protected set) and **`Ran 29 tests ... OK`** (RL + CF), both clean after
the fixes above.

## `run_baseline.py` non-destructive fix

`tests/customer_uat/run_baseline.py` previously always overwrote the
TRACKED `tests/customer_uat/baseline_results.json` and `docs/
customer_uat_sources/CUSTOMER_UAT_BASELINE_REPORT.md` as a side effect —
this corrupted an unrelated test (`tests.test_customer_calc1.
TestTrustedRates`, which reads `baseline_results.json` for specific
rate-FAQ text) whenever the harness was run mid-session for ad-hoc
measurement, and made it unsafe for the repeatable gate to call. Fixed:
default output now goes to the gitignored `tests/customer_uat/
.gate_scratch/`; the tracked files are only written with an explicit
`--commit-baseline` flag. Verified: a default run produced 47/22
(byte-identical to the SYSTEM-STATE-EMERGENCY-1 checkpoint measurement)
while `git status --porcelain` on the two tracked files stayed empty.

## Production code changed

**NONE.** Every change in this task is test infrastructure (isolation
fixes, the non-destructive harness flag) or new test/documentation content
(`tests/test_known_real_line_cases.py`, `tests/test_cross_flow_matrix.py`,
`tests/run_regression_gate.py`, this doc, `CUSTOMER_MASTER_FAILURE_
INVENTORY.md`, `tests/customer_uat/known_baseline_case_status.json`).
`DEPLOYED: NOT REQUIRED`.
