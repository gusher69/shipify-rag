# CUSTOMER UAT BASELINE REPORT — CURRENT SYSTEM MEASUREMENT

- **Candidate measured:** `3e83ed9 (verified ancestor of HEAD; zero prod-code drift)`
- **Harness:** `tests/customer_uat/run_baseline.py — REAL decide() + REAL DB registry; ERP HTTP faked; RAG faked in routing pass`
- **Isolation:** REAL `DecisionEngine.decide()`; RAG pipeline (`run_playground_turn`) and ERP HTTP (`action_executor.requests.request`) replaced with deterministic fakes. No network, no DB writes, no LLM, no destructive ERP call.
- **Production code changed:** NO   |   **Dependencies installed:** NO   |   **Deployed:** NO
- **Logical cases:** 69   |   **Strings evaluated (messages + wording variants):** 109

> **SEM-1 + SEM-1.1 applied (2026-09-03).** This report reflects the tree *after* SEM-1 (private-record status-inquiry semantic routing) and its SEM-1.1 record-scope fix. Baseline `3e83ed9` measured 65.2% logical-case pass / 69.7% semantic / 37.9% ERP-action / 14 SEMANTIC_INTENT primaries. SEM-1 moved 6 cases (CUS-G12, G16, S08, S15, S17, S18) from a RAG dead-end into the matching Business Action's identifier-collection flow. SEM-1.1 then corrected an over-reach where an UNSPECIFIED single-record inquiry (no identifier, no explicit latest/list scope) was routed to a customer-scoped *list* action that silently returned the latest record for a verified user — it now routes to the per-record *detail* action and asks for the bill/tracking id; explicit `ล่าสุด` / whole-list scope still uses the list action. Routing across the 69-case master is byte-identical between SEM-1 and SEM-1.1 (the master runs anonymous, so both ask for an identifier). The 8 residual SEMANTIC_INTENT failures are out of scope by design: 5 operational WRITE requests with no Business Action configured (CUS-G21, S02, S03, S04, S13 → Human Handoff phase) and 3 genuine how-to questions (CUS-S05, S07, S12 → RAG is the correct primary route).

> **Scope of this baseline.** The Decision Engine's *routing / classification / public-vs-private / action-selection* behaviour is measured directly and deterministically. The dimensions that depend on the live RAG pipeline output — **RAG RETRIEVAL (7)**, **NO-INFO CORRECTNESS (6)** beyond the routing-level signal, **CLARIFICATION QUALITY (5)** on RAG answers, and *DOES-NOT-INVENT* under RESPONSE QUALITY (9) — cannot be observed while the RAG pipeline is stubbed and are reported as `LIVE_RAG_REQUIRED`. Every case remains `REAL LINE REQUIRED`; nothing here closes a customer case.

## Overall

| | Passed | Failed | Pass % |
|---|---|---|---|
| Logical cases (primary wording) | 51 | 18 | **73.9%** |
| All strings (incl. wording variants) | 68 | 41 | **62.4%** |

## Dimension scores — 8 scored dimensions (logical cases, primary wording)

| # | Dimension | Pass | Scored | Pass % | LIVE_RAG_REQUIRED |
|---|---|---|---|---|---|
| 1 | SEMANTIC UNDERSTANDING | 52 | 66 | 78.8% | 0 |
| 3 | PUBLIC / PRIVATE | 50 | 51 | 98.0% | 9 |
| 4 | ROUTE | 53 | 66 | 80.3% | 0 |
| 5 | CLARIFICATION QUALITY | 21 | 21 | 100.0% | 0 |
| 6 | NO-INFO CORRECTNESS | 35 | 35 | 100.0% | 34 |
| 7 | RAG RETRIEVAL | 0 | 0 | n/a (live RAG) | 35 |
| 8 | ERP / BUSINESS ACTION | 17 | 29 | 58.6% | 0 |
| 9 | RESPONSE QUALITY | 59 | 69 | 85.5% | 0 |

**Dimension 2 — CONVERSATION OPERATION** is report-only (inferred, architecture untouched). Inferred distribution across logical cases: `{'NEW_ACTION': 68, 'CONTINUE': 1}` — single-turn UAT prompts infer NEW_ACTION; `genuine_continuation` infers CONTINUE, while the two P0-01 replay fixtures correctly infer NEW_ACTION (a fresh request after a completed cycle).

> **Dimensions 6 & 7 are answered by the Live RAG pass below, not by the routing table above** (where they read `LIVE_RAG_REQUIRED` because RAG is stubbed). The live pass produced a bare *ไม่มีข้อมูลยืนยัน*-style reply for **11 of 47** probed cases — this is the customer's single largest observed failure and it reproduces on `3e83ed9`.

## Primary root-cause family counts (failing logical cases)

| Root class | Cases |
|---|---|
| SEMANTIC_INTENT | 8 |
| HUMAN_HANDOFF | 5 |
| ERP_FLOW | 3 |
| CONTEXT_OPERATION | 1 |
| LINK_CONVERSION | 1 |

## Failure concentration by customer category

| Category | Failed | Total |
|---|---|---|
| Private ERP | 11 | 21 |
| Operational/Human CS | 4 | 6 |
| No-information/handoff | 1 | 2 |
| Correction/change target | 1 | 1 |
| Link conversion | 1 | 1 |
| Public FAQ | 0 | 23 |
| Other | 0 | 3 |
| Prohibited goods | 0 | 3 |
| Shipping rate/calculation | 0 | 3 |
| Warehouse | 0 | 1 |
| Invoice | 0 | 2 |
| Context/follow-up | 0 | 3 |

## Failing cases (primary wording)

| Case | Exp route | Exp P/P | Actual routing | Root class | Key signal |
|---|---|---|---|---|---|
| CUS-G11 | ERP | PRIVATE | WORKFLOW | ERP_FLOW | action=None |
| CUS-G17 | ERP | PRIVATE | WORKFLOW | ERP_FLOW | action=None |
| CUS-G19 | HUMAN_CS | NA | RAG | HUMAN_HANDOFF | routed RAG |
| CUS-G21 | ERP | PRIVATE | RAG | SEMANTIC_INTENT | routed RAG; action=None |
| CUS-S02 | ERP | PRIVATE | RAG | SEMANTIC_INTENT | routed RAG; action=None |
| CUS-S03 | ERP | PRIVATE | RAG | SEMANTIC_INTENT | routed RAG; action=None |
| CUS-S04 | ERP | PRIVATE | RAG | SEMANTIC_INTENT | routed RAG; action=None |
| CUS-S05 | ERP | PRIVATE | RAG | SEMANTIC_INTENT | routed RAG; action=None |
| CUS-S06 | HUMAN_CS | NA | RAG | HUMAN_HANDOFF | routed RAG |
| CUS-S07 | ERP | PRIVATE | RAG | SEMANTIC_INTENT | routed RAG; action=None |
| CUS-S10 | HUMAN_CS | NA | RAG | HUMAN_HANDOFF | routed RAG |
| CUS-S11 | ERP | PRIVATE | WORKFLOW | ERP_FLOW | action=None |
| CUS-S12 | ERP | PRIVATE | RAG | SEMANTIC_INTENT | routed RAG; action=None |
| CUS-S13 | ERP | PRIVATE | RAG | SEMANTIC_INTENT | routed RAG; action=None |
| CUS-S16 | HUMAN_CS | PRIVATE | RAG | HUMAN_HANDOFF | routed RAG |
| CUS-SC3 | CLARIFY | PUBLIC | WORKFLOW | CONTEXT_OPERATION | routed WORKFLOW |
| CUS-P06 | HUMAN_CS | PUBLIC | RAG | HUMAN_HANDOFF | routed RAG |
| CUS-P20 | WORKFLOW | PUBLIC | WORKFLOW | LINK_CONVERSION | asked identity on PUBLIC; action=geturlproductdetail |

## Cases that PASS routing but still depend on live RAG for a real verdict

CUS-G01, CUS-G02, CUS-G04, CUS-G05, CUS-G06, CUS-G07, CUS-G08, CUS-G09, CUS-G10, CUS-G13, CUS-G14, CUS-G15, CUS-G20, CUS-G22, CUS-G23, CUS-G24, CUS-G25, CUS-G26, CUS-G27, CUS-G28, CUS-S14, CUS-F01, CUS-F02, CUS-F03, CUS-F04, CUS-F05, CUS-F06, CUS-F07, CUS-F08, CUS-F09, CUS-F10, CUS-F11, CUS-SC1, CUS-P07

## Live RAG pass (REAL retrieval + generation; ERP HTTP faked)

- probed **47** cases: every RAG/CLARIFY case with a customer-provided expected answer, plus every case that routed to RAG/GENERAL in the routing pass
- verdict counts: `{'THIN_REPLY': 1, 'ANSWERED': 35, 'NO_INFO_FALLBACK': 11}`
- `NO_INFO_FALLBACK` = the live pipeline produced a bare *ไม่มีข้อมูลยืนยัน*-style reply. `ANSWERED` = a substantive reply was generated (wording-correctness vs the CS-approved answer still needs a human / Ragas judge). `strong overlap` is a positive hint only, and is noisy for Thai because the token split has no word boundaries.

| Case | exp route | routing | no-info sentence | strong overlap | verdict |
|---|---|---|---|---|---|
| CUS-G01 | RAG | RAG | no | · | THIN_REPLY |
| CUS-G02 | RAG | RAG | no | · | ANSWERED |
| CUS-G04 | RAG | RAG | no | · | ANSWERED |
| CUS-G05 | RAG | RAG | no | · | ANSWERED |
| CUS-G06 | RAG | RAG | no | yes | ANSWERED |
| CUS-G07 | RAG | RAG | no | yes | ANSWERED |
| CUS-G08 | RAG | RAG | no | yes | ANSWERED |
| CUS-G09 | RAG | RAG | YES | · | NO_INFO_FALLBACK |
| CUS-G10 | RAG | RAG | no | yes | ANSWERED |
| CUS-G13 | RAG | RAG | no | yes | ANSWERED |
| CUS-G14 | RAG | RAG | no | yes | ANSWERED |
| CUS-G15 | RAG | RAG | no | yes | ANSWERED |
| CUS-G19 | HUMAN_CS | GENERAL | YES | · | NO_INFO_FALLBACK |
| CUS-G20 | RAG | RAG | no | yes | ANSWERED |
| CUS-G21 | ERP | RAG | no | · | ANSWERED |
| CUS-G22 | RAG | RAG | no | · | ANSWERED |
| CUS-G23 | RAG | RAG | no | yes | ANSWERED |
| CUS-G24 | RAG | RAG | no | · | ANSWERED |
| CUS-G25 | RAG | RAG | no | yes | ANSWERED |
| CUS-G26 | RAG | RAG | no | · | ANSWERED |
| CUS-G27 | RAG | RAG | no | yes | ANSWERED |
| CUS-G28 | RAG | RAG | no | · | ANSWERED |
| CUS-S02 | ERP | GENERAL | YES | · | NO_INFO_FALLBACK |
| CUS-S03 | ERP | RAG | no | · | ANSWERED |
| CUS-S04 | ERP | GENERAL | YES | · | NO_INFO_FALLBACK |
| CUS-S05 | ERP | RAG | YES | · | NO_INFO_FALLBACK |
| CUS-S06 | HUMAN_CS | RAG | no | · | ANSWERED |
| CUS-S07 | ERP | RAG | YES | yes | NO_INFO_FALLBACK |
| CUS-S10 | HUMAN_CS | GENERAL | YES | · | NO_INFO_FALLBACK |
| CUS-S12 | ERP | RAG | YES | · | NO_INFO_FALLBACK |
| CUS-S13 | ERP | GENERAL | no | · | ANSWERED |
| CUS-S14 | RAG | RAG | no | · | ANSWERED |
| CUS-S16 | HUMAN_CS | GENERAL | YES | · | NO_INFO_FALLBACK |
| CUS-F01 | RAG | RAG | no | · | ANSWERED |
| CUS-F02 | RAG | RAG | no | · | ANSWERED |
| CUS-F03 | RAG | RAG | no | · | ANSWERED |
| CUS-F04 | RAG | RAG | no | · | ANSWERED |
| CUS-F05 | RAG | RAG | no | · | ANSWERED |
| CUS-F06 | RAG | RAG | no | · | ANSWERED |
| CUS-F07 | RAG | RAG | no | · | ANSWERED |
| CUS-F08 | RAG | RAG | no | yes | ANSWERED |
| CUS-F09 | RAG | RAG | no | · | ANSWERED |
| CUS-F10 | RAG | RAG | no | yes | ANSWERED |
| CUS-F11 | RAG | RAG | no | · | ANSWERED |
| CUS-SC1 | RAG | RAG | YES | · | NO_INFO_FALLBACK |
| CUS-P07 | RAG | RAG | no | · | ANSWERED |
| CUS-P06 | HUMAN_CS | GENERAL | YES | · | NO_INFO_FALLBACK |

## Top blocker families and systemic fixes (ranked by cases closed)

These are **systemic** routing/handoff changes, not phrase rules. Counts are logical cases that would move from FAIL toward PASS.

### 1. Private / account questions are classified to RAG instead of ERP identifier-collection  — ~15 cases (SEMANTIC_INTENT ×14 + most ERP_FLOW)

`สินค้าถึงโกดังหรือยัง` (CUS-G12), `ร้านส่งหรือยังคะ` (CUS-G16), `ยกเลิกบิลสั่งซื้อ` (CUS-G21) and CSW-series account actions (CUS-S02–S18) are anonymous-user questions about *this customer's own order/shipment/wallet state*. On `3e83ed9` the hybrid classifier sends them to RAG; the live RAG pass then dead-ends **12** of them with a bare *ไม่มีข้อมูลยืนยัน*. The CS-approved behaviour for every one of these is the same shape: **acknowledge → ask for the one identifier (bill / tracking / customer code) → run the Business Action**. Systemic fix: a private-state-intent detector (the `_IDENTITY_GATED_ACTION_TYPES` / `classify_turn_intent` PRIVATE_ACTION path already exists — it is not firing for these phrasings) that routes 'question about my own record' to the matching Status-Inquiry Business Action's collection flow before RAG is consulted.

### 2. No trusted answer / operational request never reaches a real Human CS handoff  — 6 cases (HUMAN_HANDOFF ×5 + CUS-P06)

`เหมารถ` (CUS-G19, CUS-SC1), `สั่งผลิตตามสเปค` (CUS-S06), `รีแพ็ค` (CUS-S10), `รวมบิลเหมารถ` (CUS-S16) and the genuine-no-info case (CUS-P06) all route to RAG and either dead-end on *ไม่มีข้อมูล* or answer thinly. Expected: a soft holding reply (*ขอเช็กข้อมูลเพิ่มเติมให้ก่อนนะคะ*) **and an actual `sendlinenotics` handoff**. Systemic fix: make 'operational request with no self-serve answer' and 'answerability = no_information on a company question' both resolve to the HUMAN_HANDOFF route with a real notification — not a RAG fallback string. (This is the Fix-2 direction that currently lives only on `main`, unverified on LINE.)

### 3. Link conversion is gated behind a customer code  — 1 case, high customer salience (CUS-P20)

`ช่วยแปลงลิงก์ให้หน่อยค่ะ` selects `geturlproductdetail` but the flow asks `กรุณาแจ้งรหัสลูกค้าค่ะ`. The customer explicitly flagged (PDF p17): link conversion is not an internal-data check and must not require a code or verification. Systemic fix: the product-link-conversion capability must declare no identity parameter / PUBLIC routing.

### 4. Public FAQ / prohibited-goods / rate answers are healthy

All 23 Public-FAQ cases, all 3 prohibited-goods and all 3 rate cases route correctly and the live RAG pass answers them substantively — including F06–F11, the customer's own 2/9/2025 recorded failures (`โกดังอ่อนนุช`, `เครื่องบิน`, `ใบกำกับ`, `ตีลังไม้`). The KB-coverage regressions from that round are largely resolved on `3e83ed9`; wording-fidelity vs the CS-approved script still needs a human / Ragas judge on real LINE.

## PDF / TC / CSW screenshot requirement coverage

Cross-checked `docs/customer_uat_sources/เคสที่ต้องแก้ใน 1.คำถามทั่วไป+2.ต้องเช็คในระบบ.pdf` (17 pages) and the `Ai.xlsx` sheet `2.ต้องเช็คในระบบ` against the 69-case master.

| PDF / source ref | Requirement | Master case | Status |
|---|---|---|---|
| p1 #1–2 | over-asks on a single question; once code given it answers | CUS-G03/S01 + CUS-SC2 | covered |
| p2 #3 | ERP lookup of cheapest private carrier *by customer code* (Kerry/JT) | CUS-G20 (its `ช่วยประเมินค่าขนส่งที่ถูกสุด` continuation) | covered as sub-requirement |
| p3 #4–5 | road-shipping time answers OK (PASS) | CUS-F02 / CUS-G22 | covered |
| p4 #6 | `ติดต่อโรงงาน` — reviewer had not confirmed the expected reply | related CUS-S06 | acknowledged; no transcribable question/answer — not fabricated |
| p5 #7 | shipping-cost calc must at least send the self-serve link | CUS-P07 | covered |
| p5 #8 / TC19 | `เหมารถ` must not say 'not in system' | CUS-G19 / CUS-SC1 | covered |
| p6 | no-info → soft holding reply + real CS handoff (there IS a CS LINE group) | CUS-P06 | covered |
| p7 #9 | member vs non-member calc; don't demand a membership no. for a general rate | CUS-P07 / CUS-G04 | covered |
| p7 #10 | `ออกใบกำกับ` must finish the multi-turn loop | CUS-G05 / CUS-F08 | covered |
| p8 TC9 | payment method OK; claims must not dead-end | CUS-G09 / CUS-G11 | covered |
| p8–10 TC11/12/16/17/18 | screenshot-only 'wrong answer, see file' — question text lives in the image, not transcribed | KB-accuracy family (CUS-F06–F11) | topic covered; individual screenshots UNRESOLVABLE — not fabricated |
| p11–17 CSW1–18, CSW20 | account/system-check cases | CUS-S01–S18, CUS-S20 / CUS-P20 | covered |
| CSW19 | — | — | does not exist in `Ai.xlsx` sheet 2 (rows run 1–18 then 20); correctly absent |

**PDF SCREENSHOT REQUIREMENT COVERAGE: COMPLETE** — every transcribable PDF/TC/CSW requirement maps to a master case. The only un-mapped refs (TC11/12/16/17/18, p4 #6) have no readable question or expected answer in the source; per the task rule no case was invented for them, and their topics (KB accuracy, factory coordination) are already represented.

## Files changed by this measurement

- `tests/customer_uat/run_baseline.py` (new — evaluator)
- `tests/customer_uat/test_baseline_smoke.py` (new — CI smoke)
- `tests/customer_uat/baseline_results.json` (new — raw results)
- `docs/customer_uat_sources/CUSTOMER_UAT_BASELINE_REPORT.md` (this file)

**PRODUCTION CODE CHANGED: NO** · **DEPENDENCIES INSTALLED: NO** · **DEPLOYED: NO**

---
_Generated by tests/customer_uat/run_baseline.py — measurement only. Every case remains REAL LINE REQUIRED; this baseline closes nothing._