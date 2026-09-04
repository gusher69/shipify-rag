# CUSTOMER MASTER FAILURE INVENTORY — REGRESSION-GATE-1

Full per-case classification of `tests/customer_uat/customer_uat_master.jsonl`
(69 logical-case rows; 68 unique `case_id`s — `CUS-S20` appears twice in the
source file, a pre-existing data-quality duplicate noted here rather than
silently fixed or hidden; both rows PASS so it does not change any count).

Measured at checkpoint **`0702e64216fcc75401920cf3f6780dc9fe179b0f`** via
`python -m tests.customer_uat.run_baseline` (default scratch mode — the
tracked `tests/customer_uat/baseline_results.json` /
`docs/customer_uat_sources/CUSTOMER_UAT_BASELINE_REPORT.md` were **not**
touched by this measurement; that tracked report was generated against an
older candidate, `3e83ed9`, and is now superseded by this inventory for
per-case accuracy — it is left in place as historical record, not deleted).

**RESULT: 47/69 logical cases PASS (68.1%). 22 FAIL.** This is a
measurement, not an acceptance claim — every case remains REAL LINE
REQUIRED. See `RELEASE READINESS` in the Regression Gate output
(`python -m tests.run_regression_gate`) for the exact, un-rounded number to
quote — never "ALL CUSTOMER UAT PASS".

## Failure classification legend

- **BUSINESS_LOGIC_BUG** — the system's current behaviour is genuinely
  wrong against the customer's own approved answer; a real product gap,
  not yet fixed, not a new regression from this task.
- **TEST_HARNESS_BUG** — the customer-facing behaviour is verified CORRECT
  (matches or closely matches the customer's approved wording); the
  `run_baseline.py` SCORING RUBRIC (not production code) has a gap that
  marks it FAIL anyway. No production code changed for these.
- **STALE_EXPECTATION** — the master's `expected_route` (or similar field)
  predates a since-shipped, tested, and (for several) REAL-LINE-verified
  feature; evidence is cited per case. The master `.jsonl` itself was left
  unedited (changing customer-sourced expectations needs its own sign-off);
  this inventory records the correction with evidence instead.
- **DEFERRED_FEATURE** — no Business Action / operational-change kind
  exists yet for this capability; a real, out-of-scope-for-this-task
  roadmap item.
- **OUT_OF_SCOPE** — explicitly out of this task's scope lock (Link
  Conversion / Admin / User Style Learning / Attachments).

## Full 69-row table

Passing cases carry no further classification (nothing to explain). Every
failing case's classification below is evidence-based — see
`tests/customer_uat/.gate_scratch/baseline_results.json` (regenerate with
`python -m tests.customer_uat.run_baseline`) for the raw signal each
classification cites.

| Case | Category | Expected route/PP | Result | Classification | Notes |
|---|---|---|---|---|---|
| CUS-G01 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G02 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G03 | Private ERP | ERP/PRIVATE | PASS | — | |
| CUS-G04 | Public FAQ | RAG/PUBLIC | **FAIL** | BUSINESS_LOGIC_BUG | "คำนวนค่าส่งให้หน่อย" correctly opens the calculator collection (asks weight/dims/method) but omits the rate-formula explanation (ทางเรือ 4500B/cbm 19B/kg, ทางรถ 6900B/cbm 35B/kg) the customer's answer leads with. Roadmap: blend the rate-FAQ sentence into the calculator's first ack. |
| CUS-G05 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G06 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G07 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G08 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G09 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G10 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G11 | Private ERP | ERP/PRIVATE | **FAIL** | DEFERRED_FEATURE | "เคลมสินค้ายังไงคะ" (missing/damaged item claim) — no Shipify-specific claims Business Action is configured; a generic warranty slot-fill fires instead, asking for a "Serial Number" that doesn't match the customer's actual required evidence (bill no. + Chinese-tracking photo + unboxing video + product photos). |
| CUS-G12 | Private ERP | ERP/PRIVATE | PASS | — | (SEM-1) |
| CUS-G13 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G14 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G15 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G16 | Private ERP | ERP/PRIVATE | PASS | — | (SEM-1) |
| CUS-G17 | Private ERP | ERP/PRIVATE | **FAIL** | BUSINESS_LOGIC_BUG | "ติดตามสถานะสินค้า" ties between multiple candidate Business Actions (order/tracking/shipment) and asks the customer to disambiguate ("พบบริการที่ตรงกับคำถามมากกว่าหนึ่งรายการ...") instead of picking a sensible default or asking one smart grouping question. Roadmap: improve multi-candidate tie-break for generic "track my product" phrasing. |
| CUS-G18 | Private ERP | ERP/PRIVATE | **FAIL** | TEST_HARNESS_BUG | Covered by CUSTOMER-ACTION-1's `operational_change_flow` (explicitly named in that file's own docstring). Actual reply "สวัสดีค่ะ แอดมินรบกวนขอสลิปการโอนเงินหน่อยนะคะ" is a near-verbatim match to the customer's approved answer. `run_baseline.py`'s `erp_action` dimension only recognizes `selected_business_action` (the BA registry), not the separate `operational_change_flow` collection mechanism — a scoring-rubric gap, not a product defect. |
| CUS-G19 | Operational/Human CS | HUMAN_CS/NA | **FAIL** | STALE_EXPECTATION | "เหมารถ" — the master predates `charter_truck_flow.py` (CUSTOMER-RAG-2/TC19), which self-serves this via a RAG FAQ answer + slot collection, never a Human CS handoff on the initial ask. This is the LOCKED, REAL-LINE-verified "Calculator initial request"-class behaviour (see `RL-TC19-01` in `tests/test_known_real_line_cases.py`); `expected_route` should read RAG/self-serve, not HUMAN_CS. |
| CUS-G20 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G21 | Private ERP | ERP/PRIVATE | **FAIL** | BUSINESS_LOGIC_BUG | "ยกเลิกบิลสั่งซื้อได้ไหม" (cancel order) needs a payment-status-branching answer (unpaid: can cancel outright; paid: must ask the store first) — a genuine ERP-flow gap. Not covered by CUSTOMER-ACTION-1's kind list (S02/S03/S04/S11/S13/S15/G18 only). Roadmap: add an order-cancellation operational kind with payment-status branching. |
| CUS-G22 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G23 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G24 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G25 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G26 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G27 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G28 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-G29 | Operational/Human CS | NA/NA | PASS | — | |
| CUS-S01 | Private ERP | ERP/PRIVATE | PASS | — | |
| CUS-S02 (CSW2) | Private ERP | ERP/PRIVATE | **FAIL** | TEST_HARNESS_BUG | Covered by CUSTOMER-ACTION-1; actual reply matches the customer's approved wording verbatim ("แอดมินขอเลขบิลสั่งซื้อของรายการนี้หน่อยนะคะ"). Same `erp_action`-dimension scoring gap as CUS-G18. |
| CUS-S03 (CSW3) | Private ERP | ERP/PRIVATE | **FAIL** | TEST_HARNESS_BUG | Same as S02 — reply near-matches customer script ("สามารถเปลี่ยนได้ค่ะ แอดมินรบกวนขอเลขบิลหน่อยนะคะ"). |
| CUS-S04 (CSW4) | Private ERP | ERP/PRIVATE | **FAIL** | TEST_HARNESS_BUG | Same as S02 — reply matches customer script verbatim. |
| CUS-S05 (CSW5) | Private ERP | ERP/PRIVATE | **FAIL** | STALE_EXPECTATION | "ถอนเงินสั่งซื้อยังไง" — customer's approved answer is a pure self-serve app/web-UI how-to (no ERP data lookup at all); already flagged in the prior (3e83ed9) baseline report's own analysis as one of "3 genuine how-to questions (S05, S07, S12) where RAG is the correct primary route." `expected_route` should read RAG, not ERP. |
| CUS-S06 (CSW6) | Operational/Human CS | HUMAN_CS/NA | **FAIL** | BUSINESS_LOGIC_BUG (= **RL-CUSTOM-SERVICE-01**, KNOWN FAILURE) | "สั่งสกรีนโลโก้ได้ไหมคะ" — source (Ai.xlsx CSW6) DOES define an approved ack+collect+coordinate process (bill + spec/qty/color/logo → store coordination), so honest no-info is NOT the correct target; current behaviour is an unstubbed RAG fallback with no collection. `operational_change_flow._KINDS` doesn't yet cover custom-production/screen-print. Tracked permanently in `tests/test_known_real_line_cases.py::TestRLCustomService01LogoPrintingRequest` (`@expectedFailure`). |
| CUS-S07 (CSW7) | Private ERP | ERP/PRIVATE | **FAIL** | STALE_EXPECTATION | "โหลดใบกำกับยังไง" — a self-serve website-navigation how-to (download an already-issued invoice), distinct from "ใบกำกับค่าสินค้าออกได้ไหม" (issuance, covered by INVOICE-REGRESSION-1/RL-INVOICE-01). Same "3 genuine how-to" family as S05/S12; `expected_route` should read RAG. KB-coverage of this exact self-serve-download content is unverified without a live RAG pass. |
| CUS-S08 | Private ERP | ERP/PRIVATE | PASS | — | (SEM-1) |
| CUS-S09 | Private ERP | ERP/PRIVATE | PASS | — | |
| CUS-S10 (CSW10) | Operational/Human CS | HUMAN_CS/NA | **FAIL** | DEFERRED_FEATURE | "รีแพ็คค่ะ" (repack + consolidate) is an operational WRITE action with no configured kind; not in CUSTOMER-ACTION-1's list. Roadmap: add a repack/consolidate operational kind. |
| CUS-S11 (CSW11) | Private ERP | ERP/PRIVATE | **FAIL** | TEST_HARNESS_BUG | Covered by CUSTOMER-ACTION-1; reply asks for the transport bill as its script requires. Same `erp_action`-dimension scoring gap. |
| CUS-S12 (CSW12) | Private ERP | ERP/PRIVATE | **FAIL** | STALE_EXPECTATION (+ DEFERRED_FEATURE note) | "ถอนเงินขนส่งยังไงคะ" — how-to, same S05/S07 family; `expected_route` should read RAG. The customer's own answer additionally requires a brand-conditional branch (SP vs FT give different steps) that a plain RAG answer doesn't yet implement — noted as a separate, smaller DEFERRED_FEATURE (conditional-branch answering) for the live-RAG follow-up, not invented as a false PASS here. |
| CUS-S13 (CSW13) | Private ERP | ERP/PRIVATE | **FAIL** | TEST_HARNESS_BUG | Covered by CUSTOMER-ACTION-1; reply asks to check/delete the duplicate bill as scripted. Same scoring gap. |
| CUS-S14 | Other | RAG/PUBLIC | PASS | — | |
| CUS-S15 (CSW15) | Private ERP | ERP/PRIVATE | **FAIL** | TEST_HARNESS_BUG | Covered by CUSTOMER-ACTION-1; reply matches ("แอดมินช่วยตรวจสอบความถูกต้องให้ค่ะ..."). Same scoring gap. |
| CUS-S16 (CSW16) | Operational/Human CS | HUMAN_CS/PRIVATE | **FAIL** | BUSINESS_LOGIC_BUG | "รวมบิลเหมารถค่ะ" (consolidate multiple bills into one charter) — `interpret()`'s CHARTER_TRUCK family doesn't recognize this phrasing at all (falls through to plain RAG, doesn't even enter `charter_truck_flow`); a distinct bill-consolidation sub-capability, not yet built. Roadmap item, not fixed in this task per scope lock. |
| CUS-S17 (CSW17) | Private ERP | ERP/PRIVATE | PASS | — | = **RL-TRACK-TH-01**. Routes to `searchdatatracking` as an identity-gated private read; does NOT ask for a Chinese tracking number (the customer's explicit hard constraint). First-ask wording (CustCode-first vs the script's bill-first) differs slightly but matches the same identity-gated pattern used throughout the passing private-ERP cases — not registered as a failure. |
| CUS-S18 | Private ERP | ERP/PRIVATE | PASS | — | (SEM-1) |
| CUS-S20 | Other | NA/NA | PASS | — | Duplicate row in the source `.jsonl` (appears twice, both PASS) — see header note. |
| CUS-F01 | Prohibited goods | RAG/PUBLIC | PASS | — | |
| CUS-F02 | Shipping rate/calc | RAG/PUBLIC | PASS | — | |
| CUS-F03 | Shipping rate/calc | RAG/PUBLIC | PASS | — | |
| CUS-F04 | Prohibited goods | RAG/PUBLIC | PASS | — | |
| CUS-F05 | Prohibited goods | RAG/PUBLIC | PASS | — | |
| CUS-F06 | Warehouse | RAG/PUBLIC | PASS | — | |
| CUS-F07 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-F08 | Invoice | RAG/PUBLIC | PASS | — | |
| CUS-F09 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-F10 | Invoice | RAG/PUBLIC | PASS | — | |
| CUS-F11 | Public FAQ | RAG/PUBLIC | PASS | — | |
| CUS-SC1 | Operational/Human CS | RAG/PUBLIC | PASS | — | |
| CUS-SC2 | No-information/handoff | ERP/PRIVATE | PASS | — | |
| CUS-SC3 | Correction/change target | CLARIFY/PUBLIC | **FAIL** | TEST_HARNESS_BUG | "54x12x43" (bare dimensions) is defined by its own `expected_behavior` as a MULTI-TURN case ("previous bot turn asked for weight and size") but carries no `replay_fixture`, so `run_baseline.py` runs it with EMPTY history — the engine cannot know a shipping-estimate collection is pending. The original REAL-LINE bug this case captured (`ai_actually_answered`: demanded phone-number identity verification, linked `fix1:54x12x43`) is CONFIRMED FIXED: the current bare-history reply does NOT ask for phone/customer code (the customer's explicit hard constraint), it just can't disambiguate a lone dimension string with zero context, which is expected. Roadmap: give this case a `replay_fixture` carrying the prior "weight+size?" turn so it can be scored correctly. |
| CUS-P07 | Shipping rate/calc | RAG/PUBLIC | **FAIL** | STALE_EXPECTATION | "คิดค่านำเข้าให้หน่อย" — customer's answer says "at least send the self-serve rate link"; the current calculator-collection flow (asks weight/dims/method to compute a real number) exceeds that minimum bar and is the LOCKED, REAL-LINE-verified "Calculator initial request" behaviour. `expected_route` should allow WORKFLOW, not RAG-only. |
| CUS-P06 | No-information/handoff | HUMAN_CS/PUBLIC | **FAIL** | TEST_HARNESS_BUG | Genuine no-info-in-KB case, built to exercise Fix-2 (`unsupported_company_fact` → real Human CS handoff via the Answerability-Gate). The routing-only harness stubs RAG to always answer something, so Fix-2's no-info branch — and this case — can never fire in this measurement pass by construction (documented in the harness's own report: "the routing pass stubs RAG so `unsupported_company_fact` is never set"). Verified correct separately by `tests.test_customer_uat_fix2_unsupported_fact_handoff` (its own dedicated, passing suite). |
| CUS-P20 | Link conversion | WORKFLOW/PUBLIC | **FAIL** | OUT_OF_SCOPE | CUSTOMER-LINK-1 — explicitly out of scope for REGRESSION-GATE-1 (scope lock: "no Link Conversion"). Current behaviour (`geturlproductdetail` asks for a customer code) is the known, previously-audited issue CUSTOMER-LINK-1 was opened to fix; that task remains open and unstarted. |
| CUS-RL-genuine_continuation | Context/follow-up | ERP/NA | PASS | — | |
| CUS-RL-p0_01_repeat_after_completed_cycle | Context/follow-up | WORKFLOW/NA | PASS | — | |
| CUS-RL-p0_01_stale_cycle | Context/follow-up | WORKFLOW/NA | PASS | — | |

## Classification totals (22 failures)

| Classification | Count | Cases |
|---|---|---|
| TEST_HARNESS_BUG | 8 | G18, S02, S03, S04, S11, S13, S15, P06 |
| STALE_EXPECTATION | 6 | G19, S05, S07, S12, SC3\*, P07 |
| BUSINESS_LOGIC_BUG | 5 | G04, G11\*, G17, G21, S06\*, S16 |
| DEFERRED_FEATURE | 2 | S10 |
| OUT_OF_SCOPE | 1 | P20 |

\* SC3 is TEST_HARNESS_BUG (fixture gap) — listed once above, not double-counted.
\* G11 and S06 are also DEFERRED_FEATURE in nature (no matching capability
configured); classified BUSINESS_LOGIC_BUG/DEFERRED_FEATURE per the closest
fit above — see each row's Notes for the precise reasoning, counts are not
meant to be read as mutually exclusive buckets beyond the row-level detail.

**None of the 22 failures were reclassified to OUT_OF_SCOPE merely to
improve the count** — 21 of 22 have a concrete, evidenced root cause
(harness rubric gap, stale master expectation, or a real product gap);
only CUS-P20 is OUT_OF_SCOPE, and it is OUT_OF_SCOPE because it is the
explicit subject of a separate, already-opened, not-yet-started task
(CUSTOMER-LINK-1), not because it was hard to classify.

## Regression Gate vs Release Readiness (do not conflate)

- **Regression Gate** (`python -m tests.run_regression_gate`): diffs this
  measurement case-by-case against `tests/customer_uat/
  known_baseline_case_status.json` (the locked-checkpoint snapshot). A case
  flipping PASS→FAIL blocks deployment. A case that was already failing and
  stays failing does **not** block deployment — it is tracked above.
- **Release Readiness**: **47/69 (68.1%)** logical cases pass right now.
  This is not "ALL CUSTOMER UAT PASS" and must never be reported as such.
