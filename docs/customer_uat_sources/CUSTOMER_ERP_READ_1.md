# CUSTOMER-ERP-READ-1 — private ERP read error taxonomy

Audited the 22 deferred ERP / private-read customer UAT cases
(`G03,G11,G12,G16,G17,G18,G21,S01–S05,S07–S09,S11–S13,S15,S17,S18,SC2`)
and the 15 required identifier-state scenarios against production
`439f317` in the container (mocked verified binding `FT3182` for LINE
user `Uc5f5717bc090934f9eaa067513388178`, mocked ERP).

## The 22 deferred cases

| Bucket | Cases | Root class | Fix |
|---|---|---|---|
| Pure private-read, identifier missing → asks for the one identifier (state B) | G03, G12, G16, G17, S01, S08, S17, S18 | ALREADY_PASS (MISSING_INPUT_HANDLING) | — |
| How-to procedure — RAG is the correct primary route | G11 (claim), S05 (withdraw purchase wallet), S07 (invoice download), S12 (withdraw shipping wallet, brand-dependent) | ALREADY_PASS | — |
| "Send a slip / policy" soft-CS reply | G18 (slip), G21 (cancel policy) | CONTEXT_STATE / record-only | deferred — no Business Action, WRITE-adjacent |
| Operational WRITE request (scope-locked: no write Business Actions) | S02, S03, S04, S09, S11, S13, S15 | deferred to ACTION/WORKFLOW | — |
| Self-verification loop | SC2 | ALREADY_PASS (IDENTITY-0 already ends the loop with one Human CS handoff) | — |

No deferred case needed a code change. The two fixes below come from the
**identifier-state gaps** (states C and D) the required-test matrix
exposed — they are systemic to every private read that supplies a bad or
absent bill number (G03 / G12 / G16 / G17 / S01 / S08 / S17 / S18).

## Error taxonomy — before / after

| State | Trigger | Before `439f317` | After this change |
|---|---|---|---|
| SUCCESS | valid id, ERP returns the record | real ERP fields | unchanged |
| MISSING_INPUT | `ของผมถึงไหนแล้ว` (no id) | asks for the identifier | unchanged |
| **MALFORMED_IDENTIFIER** | `FT31822026072` (13 chars — code-shaped but incomplete) | **accepted, sent to ERP** → came back not-found | **"เลขที่แจ้งมา (FT31822026072) ดูเหมือนจะไม่ครบถ้วนค่ะ รบกวนตรวจสอบและส่ง…แบบเต็มอีกครั้งนะคะ"** — no ERP call, no Human CS |
| **VALID_NOT_FOUND** | `FT318220260726999` (valid shape), ERP returns `data:{Shipment:null}` / `{}` / `null` | **"ดำเนินการเรียบร้อยค่ะ"** (implies success) | **"ไม่พบข้อมูลรายการสำหรับเลขที่ … ในระบบค่ะ รบกวนตรวจสอบ…อีกครั้งแล้วแจ้งมาใหม่นะคะ"** |
| UNAUTHORIZED | unverified / identity-switch / stale SP1008 | deterministic denial → self-verification | unchanged |
| ERP_FAILURE | HTTP 500 / timeout / exception | "ขอโทษด้วยค่ะ ไม่สามารถดำเนินการได้ในขณะนี้ รบกวนลองใหม่อีกครั้งนะคะ" | unchanged — kept distinct from NOT_FOUND |

## Fixes (all in `services/decision_engine.py`, deterministic, no new infrastructure, no migration)

Both fixes are **scoped by `action_key` to the three private ERP READ
status-inquiry actions** — `_ERP_READ_STATUS_ACTIONS = {"searchdatashipment",
"searchdataorder", "searchdatatracking"}` — so a WRITE / notification
flow (`requestshippingaddresschange`) and any synthetic test action are
never touched.

1. **MALFORMED (state C)** — `_incomplete_shipment_code(message,
   expecting_param, action_key)` + `_incomplete_identifier_prompt(param,
   token)`. Fires only for `action_key == "searchdatashipment"` and the
   `ShipmentCode` slot: a bill-shaped token that is too short to be
   complete (`[A-Za-z]{2}\d{6,12}` — real Shipify codes are a 2-letter
   prefix + 15 digits; the customer's documented incomplete example
   `FT31822026072` is 2 + 11) →
   - in the collection re-ask branch, the prompt becomes the
     "send the complete number" wording;
   - as a pre-execution guard on the *collected* `ShipmentCode` value
     (the loose `^[A-Za-z]{2}\d{10,}$` `validation_pattern` accepts
     `FT`+11 digits), the flow returns the same wording instead of
     calling the ERP.
   Checks the *bound slot value* / a bill-shaped token, never a bare
   `CustCode`-shaped token like `SP1008` (prefix + 4 digits — below the
   6-digit floor). Format-only, never brand-specific, never a phrase rule.

2. **VALID_NOT_FOUND (state D)** — in the API-success branch, gated on
   `selected.action_key in _ERP_READ_STATUS_ACTIONS`: when a
   customer-supplied `ShipmentCode` / `OrderCode` / `Tracking` lookup
   returns HTTP-OK with an empty / all-null `mapped_fields`, return the
   "not found, re-check the number" reply instead of the generic
   `_compose_natural_reply` fallback ("ดำเนินการเรียบร้อยค่ะ"). Mirrors
   the existing empty-result wording for LIST actions
   (`_aggregate_list_reply`). Never fires for an aggregation request,
   `getdatacustomer`, or any write flow.

Authorization is untouched — `services/authorization_service.py`
(verified binding + resource-owner CustCode match) remains the ONE
deterministic gate; Semantic-First never influences it.

## Required-test results (container, mocked ERP + binding)

| # | Scenario | Verdict |
|---|---|---|
| 1 | verified private shipment read (`FT318220260726001`) | PASS — status "รับเข้าที่จีน", tracking, total |
| 2 | unverified private shipment read | PASS — never returns ERP data; self-verification |
| 3 | missing shipment identifier | PASS — asks; no no-info, no Human CS |
| 4 | malformed `FT31822026072` | **FIXED** — "incomplete, send the full number"; no ERP, no Human CS |
| 5 | valid-format but ERP not found | **FIXED** — "not found, re-check"; not "done", not "system error" |
| 6 | mocked ERP failure (500 / exception) | PASS — temporary system-error reply, distinct from not-found |
| 7 | wallet read | PASS — Purchase Wallet 69,616.82 |
| 8 | private coupon ownership (`[]`) | PASS — "ไม่พบข้อมูลคูปองค่ะ", authorized |
| 9 | registered phone read | PASS — masked `****5678` |
| 10 | stale SP1008 must not authorize | PASS — cached id not auto-used (P8.2); SP1008 never authorizes |
| 11 | no auto-latest shipment | PASS — asks for the id, never silently returns latest |
| 12 | context switch to public transit-time mid-flow | PASS — 14–20 days |
| 13 | public/private boundary (coupon / phone / shipment vs FAQ) | PASS — all 4 |
| 14 | Semantic-First families | PASS — routing correct (private-state gate carries the reads) |
| 15 | Fix-2 true no-info | PASS — HUMAN_HANDOFF unchanged |

## Status

**CODE PASS / DEPLOYED / READY FOR FINAL MANUAL UAT.** No REAL LINE
testing performed. Customer acceptance not declared.
