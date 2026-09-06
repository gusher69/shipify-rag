# PHASE 2 — SOURCE COVERAGE AUDIT

> **AUDIT ONLY.** No production code, no production data, no KB, no routing, no
> ERP/action config was changed. `PHASE-2-SOURCE-COVERAGE-AUDIT-1`.
>
> Production SHA at audit time: `0937d71951f1cdf2a073d97357f562092b744016` — **UNCHANGED**.
> Phase 1 (customer workflows) is COMPLETE. Next: Phase 3 — RAG Coverage Completion.

---

## 1. Executive Summary

The customer source set is **fully inventoried and mapped**. Every logical
customer requirement traces to a `customer_uat_master.jsonl` row (or is
recorded here as *missing from master*), and every requirement is assigned a
future phase.

Headline numbers (recalculated this phase, not copied from history):

| Metric | Value |
|---|---|
| Source files inspected | 8 (2 XLSX, 1 PDF, 5 images) |
| XLSX sheets inspected | 8 (`Ai.xlsx` ×7, `ปัญหา…` ×1) |
| PDF pages inspected | 17 / 17 (text-layer + image count; **~40 embedded screenshots NOT pixel-rendered — poppler unavailable in this environment**, see §10) |
| Standalone screenshots inspected | 5 / 5 (3 case, 2 artifact) |
| Master JSONL rows | **69** |
| Unique `case_id` | **68** |
| Duplicate `case_id` | **1** — `CUS-S20` (two rows, same CSW20 source row, two wordings/answers) |
| Logical customer requirements | **~66** (68 unique IDs − 3 replay-fixture rows that are internal regression fixtures, not customer feedback, + 1 for the S20 second facet; see §2) |
| Source requirements MISSING from master | **2 candidates** (cheapest-carrier compare; SP-withdrawal image attachment) + several *comment-only* rules (§9) |
| Master cases WITHOUT source backing | **0** (all 68 unique trace to Ai.xlsx / ปัญหา / PDF / screenshot / real-line fixture) |
| RAG gaps | **6** (§7) |
| ERP/API capability gaps | **9** (§8) — all EXTERNAL_API_DEPENDENCY |
| Workflow gaps (source workflow not in the known list) | **1–2 candidates** (§8, cheapest-carrier compare; membership-vs-general calc split already handled) |
| Source conflicts | **2** (§10 — S12 route ERP-vs-RAG; S07 route ERP-vs-RAG vs the F10 twin) |
| Test-coverage gaps | **large** — the Phase-1 per-workflow focused tests live only in a scratchpad and are **not committed** (§11) |
| Deferred bugs consolidated | **17** (§12) |
| FINAL_REAL_LINE_UAT_REQUIRED | **69 / 69** (`requires_real_line: true` on every row) |
| CUSTOMER_CLARIFICATION required | **3** (§10) |
| EXTERNAL_API_DEPENDENCY | **9** (§8) |

**Phase-2 exit decision: COMPLETE** (with the one honest caveat that the ~40
in-PDF screenshots were audited via their text-layer annotations + `Ai.xlsx`
cross-reference, not pixel-rendered — the same limitation the existing
`CUSTOMER_UAT_SOURCE_MANIFEST.md` records; the annotations name every case
(TC/CSW ids) and all resolve to sheet rows already captured).

---

## 2. Source Inventory

### 2.1 `Ai.xlsx` — primary case catalogue (7 sheets)

| Sheet | Rows | Type | Maps to |
|---|---|---|---|
| `Api Overall` (31) | — | project meta / API design | not a case source |
| `all` (7) | — | project meta | not a case source |
| `1.ถามเบื้องต้น` | data rows r3–r31 = **cases No.1–29** | Public-facing FAQ catalogue w/ CS-approved answer + type (RAG/API/Human CS) + media refs + `หมายเหตุ` + `API ที่ต้องขอ` | **CUS-G01 … CUS-G29** |
| `2.ต้องเช็คในระบบ` | data rows r4–r24 = **cases No.1–18 + No.20** (No.19 absent → no CSW19) | Account-deep CS workflow: current CS steps, stage-2 template, `API Input` + `API Output ที่คาดหวัง`, priority notes | **CUS-S01 … CUS-S18, CUS-S20 (×2)** |
| `3.Policy` | r6–r7 = 2 rows | policy source URLs (`/PagePolicy`, `/article_detail/66`) | KB source refs (not test cases) |
| `API Summary for Client` | — | API design | not a case source |
| `API` | `SecretCode` row = a **real 64-char credential** | — | **read, recognised, MASKED — never stored** |

Verdict tallies stamped by the customer in the sheet footer rows:
* `1.ถามเบื้องต้น` (29): 13 = *answer OK, image not yet attached in LINE*; 7 = *wrong, fix per file*; 9 = *correct*.
* `2.ต้องเช็คในระบบ` (~19): 12 = *wrong, fix per file*; 7 = *correct*.

### 2.2 `ปัญหาที่เจอในการตอบ (1).xlsx` — real 2/9/2025 UAT run (1 sheet `สรุปการเทรน AI`)

11 data rows r2–r12 → **CUS-F01 … CUS-F11**. Columns: `สิ่งที่ต้องเทรนเพิ่ม` (what to train / note), `คำถามที่ทดสอบ`, `สิ่งที่ AI ตอบ` (bad answer), `แนวทางที่ต้องการ` (desired), `TEST 2/9/2025` (verdict).
Recorded verdicts: **F05 = FAIL** (large-quantity battery framing), **F08 = PASS 50% / FAIL 50% / ไม่ครบลูป**, F01/F02/F03/F04/F06/F07/F09/F10/F11 = PASS.

### 2.3 `เคสที่ต้องแก้ใน 1.คำถามทั่วไป+2.ต้องเช็คในระบบ.pdf` — reviewer annotations (17 pages)

Thin text layer + ~40 embedded chat screenshots. Text-layer annotations decoded per page:

| Page | Annotation (decoded) | Resolves to |
|---|---|---|
| 1 | "asked one question — why answer like this — should acknowledge & continue"; "when given the code, answers OK" | SC2/SC3 identity-loop; general "don't dead-end" |
| 2 | **"must go into ERP … check shipping cost in the system by customer code — which carrier is cheapest e.g. Kerry/JT — we need an API to check shipping cost by customer code"** | **NEW requirement — cheapest private-carrier compare by CustCode** (candidate MISSING, §8) |
| 3 | item 4 PASS; item 5 road-shipping PASS | F02 / calc |
| 4 | "contact factory >> re-check answer with customer" | CSW6 custom production |
| 5 | item 7: calc answer fails — **at minimum send `https://www.shipify.co.th/Rate`**; item 8: charter **must not say 'not in system' — see TC19** | CUS-P07 / CUS-G04 ; CUS-SC1 / G19 |
| 6 | "no data in system → forward to CS — which channel? another LINE group?"; **should answer "ขอเช็กข้อมูลเพิ่มเติมให้ก่อนนะคะ"** | CUS-P06 handoff wording |
| 7 | item 9: **calc has 2 cases — member vs general — do NOT ask for membership number immediately unless it's personal data/real balance; separate general vs account-deep**; item 10: **ออกใบกำกับ answers incomplete loop — must ask product then close the loop** | CUS-SC3 / calc ; **CUS-G05 / CUS-F08 incomplete loop** (§7) |
| 8 | TC9 payment how-to OK but **the claims part should NOT appear** (prohibited-answer rule); TC11 wrong → use file | G09 prohibited-answer ; G-series |
| 9 | TC12, TC16 wrong → use file | G-series |
| 10 | TC17, TC18 wrong → use file | G-series |
| 11 | `2.ต้องเช็คในระบบ`; CSW1 wrong — must use given answer, ask bill then forward to admin; CSW2 header | CUS-S01 / CUS-S02 |
| 12 | CSW2 / CSW5 / CSW6 — wrong → use given answer, ask bill then forward | CUS-S02 / S05 / S06 |
| 13 | CSW8 wrong → use given answer; **CSW10 — AI must check ERP for item count then answer per file, receive-and-forward to CS**; CSW11 — per file, receive-and-forward to CS | CUS-S08 / S10 / S11 |
| 14 | **CSW12 — per file *with a withdrawal image attached*; check brand SP/FT; must ask code back first**; SP vs FT answers verbatim | **CUS-S12 image attachment** (candidate MISSING, §8) |
| 15 | CSW16 per file + notify CS; **CSW17 — per file, but a person must search; can we pull the API we were given? ask-back must ask tracking BEFORE saying "no"** | CUS-S16 / **CUS-S17** |
| 16 | CSW18 — per file; **whether there is or isn't, answer per file; if never ordered must be able to pull "no shipment record"** | **CUS-S18** (relates to the CSW18 deferred answer-shape bug) |
| 17 | **CSW20 — must actually convert the link; link conversion is NOT internal-data checking — remove the security / identity requirement** | **CUS-P20 / G29 / S20** public, no-auth |

### 2.4 Standalone screenshots (5)

| Image | Content | Case |
|---|---|---|
| `messageImage_1788318551530.jpg` | `มีบริการเหมารถไหมคะ` → AI wrongly `ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องบริการเหมารถในระบบค่ะ` | **CUS-SC1** (linked → CUS-G19). Fixed Phase 1 (G19/SC1). |
| `messageImage_1788321460773.jpg` | Self-service identity-verification **LOOP** — phone → "doesn't match" → email → "cannot verify, contact staff" → next private question → re-asks phone → "doesn't match" → email → loops | **CUS-SC2** (`status: NEEDS_INTERPRETATION`). NOT a Phase-1 workflow — an **AUTH_CUSTOMER_BINDING / verification-flow defect**. |
| `messageImage_1788321878908.jpg` | `ค่าขนส่งคิดยังไง` → bot asks weight+size → `54x12x43` → AI wrongly asks for `เบอร์โทรที่ผูกกับบัญชีลูกค้า` on a PUBLIC calc | **CUS-SC3**. Calculator "don't ask for identity on a general calc". |
| `messageImage_1788323949934.jpg` | Spreadsheet cell listing attachment filenames | artifact — no case |
| `messageImage_1788324389650.jpg` | Screenshot of `Ai.xlsx` `1.ถามเบื้องต้น` in Google Sheets (confirms rows 22–31 + verdict tally) | artifact — no case |

---

## 3. Source → Case ID Mapping

Every logical customer requirement and its master representation.

| Case ID | Source (file · location) | Coverage | Notes |
|---|---|---|---|
| CUS-G01 … CUS-G29 | `Ai.xlsx` `1.ถามเบื้องต้น` r3–r31 (No.1–29) | **COVERED_EXACTLY** (29/29) | wording, answer, type, media, `หมายเหตุ`, `API ที่ต้องขอ` all present in master via `user_message` / `customer_provided_expected_answer` / `expected_route`; **the `API ที่ต้องขอ` request list is NOT copied into master** (captured here in §8). |
| CUS-S01 … CUS-S18 | `Ai.xlsx` `2.ต้องเช็คในระบบ` r4–r21 (No.1–18) | **COVERED_EXACTLY** (18/18) | stage-2 template + `API Input`/`API Output` captured as `api_input_hint` (input only); the `API Output ที่คาดหวัง` field list is NOT in master (captured in §8). |
| CUS-S20 (×2) | `Ai.xlsx` `2.ต้องเช็คในระบบ` r22 + r23 (both "20.0" / CSW20) | **DUPLICATE (SAME_INTENT_DIFFERENT_FACET)** | r22 `ตัวอย่างแปลงลิงก์` (raw links pasted) + r23 `ช่วยแปลงลิงก์ให้หน่อยค่ะ` (conversion request + shipify.co.th output). Same requirement (Link Conversion / CSW20). Same `case_id` — a data-hygiene issue, **record only, do not merge/renumber this phase**. |
| CUS-F01 … CUS-F11 | `ปัญหา… .xlsx` `สรุปการเทรน AI` r2–r12 | **COVERED_EXACTLY** (11/11) | `ai_actually_answered`, `customer_provided_expected_answer`, `recorded_verdict`, `customer_note` present. |
| CUS-SC1 | screenshot 1788318551530 | **COVERED_EXACTLY** | `linked_cases: [CUS-G19]` |
| CUS-SC2 | screenshot 1788321460773 | **COVERED_PARTIALLY** | `status: NEEDS_INTERPRETATION`; the verification-loop defect is captured but the desired behaviour is not fully specified in master. |
| CUS-SC3 | screenshot 1788321878908 | **COVERED_EXACTLY** | `expected_route: CLARIFY` |
| CUS-P06 | PDF page 6 | **COVERED_EXACTLY** | genuine-no-info → `ขอเช็กข้อมูลเพิ่มเติมให้ก่อนนะคะ` + `sendlinenotics` handoff |
| CUS-P07 | PDF page 5 item 7 / page 7 item 9 | **COVERED_EXACTLY** | calc → send `/Rate` link when not computable |
| CUS-P20 | PDF page 17 (CSW20) + `Ai.xlsx` 1.ถามเบื้องต้น #29 | **COVERED_EXACTLY** | `expected_route: WORKFLOW`, PUBLIC, **no CustCode / no identity** |
| CUS-RL-genuine_continuation | `tests/fixtures/real_line/genuine_continuation.json` | **internal regression fixture** — not customer feedback |
| CUS-RL-p0_01_repeat_after_completed_cycle | `tests/fixtures/real_line/p0_01_repeat_after_completed_cycle.json` | **internal regression fixture** |
| CUS-RL-p0_01_stale_cycle | `tests/fixtures/real_line/p0_01_stale_cycle.json` | **internal regression fixture** |

---

## 4. Requirement Coverage Matrix

One row per **logical customer requirement**. Status is against production `0937d71` + live config inspected read-only this phase.

| Case ID | Src sheet/pg | Source wording | Requirement summary | Expected route | Pub/Priv | Required inputs | Media | Impl type | Current component | Current status | Test coverage | RL req'd | Gap | Sev | Next phase |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| CUS-G01 | Ai `1` r3 | ขอที่อยู่โกดังหน่อย | TH warehouse address (2 sites) + hours + map | RAG | PUB | — | maps + FT/SP imgs | PUBLIC_RAG | KB `บัญชี`/warehouse chunks; `answer_planner.warehouse_*` | IMPLEMENTED_COMPLETE (asks TH/CN) | `test_customer_rag1_pickup_location`, `test_answer_planner` | Y | image attach pending (13-tally) | Low | PHASE_3_RAG_COVERAGE (image attach) |
| CUS-G02 | Ai `1` r4 | ขอที่อยู่โกดังจีน | CN warehouse address SP/FT + step imgs | RAG | PUB | — | 2 imgs | PUBLIC_RAG | KB china-warehouse chunk | IMPLEMENTED_COMPLETE | `test_customer_rag1_pickup_location` | Y | image attach pending | Low | PHASE_3_RAG_COVERAGE |
| CUS-G03 | Ai `1` r5 | สินค้าจะเข้าไทยตอนไหน | ETA-to-TH by bill/tracking | ERP | PRIV | bill_no / tracking | — | PRIVATE_ERP_READ | `decision_engine` PSI → `searchdatashipment`/`searchdatatracking`; `_compose_shipment_followup_reply` (ETA branch) | IMPLEMENTED_PARTIAL — ERP returns `Status` not a real `estimated_arrival_th`; "no confirmed ETA in system" honest line | `test_customer_erp_read1`, `test_customer_track_th1*` | Y | no trusted `estimated_arrival_th` field | Med | EXTERNAL_API_DEPENDENCY |
| CUS-G04 | Ai `1` r6 | ค่าขนส่งคิดยังไง / คำนวนค่าส่ง | shipping-cost calc (vol vs weight, road/sea rates) | RAG | PUB | weight + dims | — | CALCULATOR + RAG | `decision_engine` calculator flow; rate KB | IMPLEMENTED_COMPLETE (multi-turn calc) | `test_customer_calc1`, `test_customer_calc11_conversation_state`, `test_excel_calculator`, `test_calculator_regression_2` | Y | — | — | NO_ACTION_REQUIRED |
| CUS-G05 | Ai `1` r7 | ออกใบกำกับได้ไหม | invoice-issuance service Q → "yes, what product?" then close loop | RAG | PUB | product type (+customer type/import mode per PDF pg7) | — | PUBLIC_RAG (multi-turn slot) | `conversation_semantics` INVOICE family; KB invoice chunks (`ff288877`,`99390831`) | IMPLEMENTED_PARTIAL — **incomplete loop** (PDF pg7 item 10 / F08 verdict) | `test_customer_invoice1`, `test_invoice_product_regression2` | Y | multi-turn "ask product → confirm can issue" loop not closed | Med | PHASE_5_DEFERRED_FIX |
| CUS-G06 | Ai `1` r8 | CBM คิวคืออะไร | CBM definition + formula | RAG | PUB | — | 1 img | PUBLIC_RAG | KB | IMPLEMENTED_COMPLETE | `test_customer_rag_audit` | Y | — | — | NO_ACTION_REQUIRED |
| CUS-G07 | Ai `1` r9 | มีขั้นต่ำในการสั่งไหม | no minimum (except CN shop) | RAG | PUB | — | — | PUBLIC_RAG | KB `payment_policy` | IMPLEMENTED_COMPLETE | `test_customer_rag_audit` | Y | — | — | NO_ACTION_REQUIRED |
| CUS-G08 | Ai `1` r10 | สินค้าที่ห้ามนำเข้ามีอะไรบ้าง | prohibited-goods list | RAG | PUB | — | — | PUBLIC_RAG | KB `prohibited_goods` | IMPLEMENTED_COMPLETE | `test_customer_rag_audit`, `test_evidence_classifier` | Y | can't enumerate sub-categories (F01/F04/F05 need reasoning-over-list) | Med | PHASE_3_RAG_COVERAGE (+ PHASE_5 for reasoning) |
| CUS-G09 | Ai `1` r11 | วิธีการชำระบิลสั่งซื้อ / **ชำระค่าสินค้ายังไง** | purchase-bill payment how-to + QR | RAG | PUB | — | 2 imgs | PUBLIC_RAG | KB `506370f4`; `_FUZZY_CORRECTION_PHRASE_GUARDS` (fixed) | IMPLEMENTED_COMPLETE (spell-guard added, `45d3f00`) | `test_customer_rag_audit` | Y | image attach pending; PDF pg8 "claims part must NOT appear" | Low | PHASE_3_RAG_COVERAGE |
| CUS-G10 | Ai `1` r12 | บิลขนส่งชำระได้เลยไหม | pay shipping bill only after goods reach TH | RAG | PUB | — | — | PUBLIC_RAG | KB `5d168a64` timing FAQ; `_invoice_issuance_branch_applies` (fixed to deterministic-only) | IMPLEMENTED_COMPLETE (fixed `45d3f00`) | `test_customer_rag_audit` | Y | LLM family-guess previously mislabelled INVOICE | Low | PHASE_6_REAL_LINE_UAT (retrieval confidence) |
| CUS-G11 | Ai `1` r13 | ได้รับสินค้าไม่ครบ / เคลมสินค้ายังไง | missing/damaged-item claim: collect bill + CN-track photo + unbox video + product photos; open claim | ERP | PRIV | bill_no, evidence set | — | OPERATIONAL_WORKFLOW + BUSINESS_ACTION_WRITE (`POST /claims`) | `operational_change_flow.missing_item_claim` (collect + Human CS) | IMPLEMENTED_SAFE_FALLBACK — collects evidence list, Human CS; **no `POST /claims` action** | `test_customer_action1` (recognizer) | Y | no claims WRITE API | Med | EXTERNAL_API_DEPENDENCY |
| CUS-G12 | Ai `1` r14 / S18 note | สินค้าถึงโกดังหรือยัง | warehouse-received check by CN tracking | ERP | PRIV | tracking_cn (or bill) | — | PRIVATE_ERP_READ | `decision_engine._compose_shipment_followup_reply` warehouse branch; `_WAREHOUSE_ARRIVED_STATUS_RE` | IMPLEMENTED_COMPLETE (Phase 1 `b5f7d80` — split-flow closed) | `test_customer_track_th1_1`, scratchpad `test_g12` (uncommitted) | Y | ambiguous-status semantics over-commit "ยังไม่ถึงโกดัง" | Low-Med | PHASE_5_DEFERRED_FIX |
| CUS-G13 | Ai `1` r15 | ระยะเวลาการส่งจากร้านจีน–โกดังจีน | 2–4 days shop→CN warehouse | RAG | PUB | — | — | PUBLIC_RAG | KB | IMPLEMENTED_COMPLETE | `test_customer_rag_audit` | Y | — | — | NO_ACTION_REQUIRED |
| CUS-G14 | Ai `1` r16 | ชำระบัตรเครดิตได้ไหม | credit-card policy: min ฿500/bill, +3%, no VAT invoice | RAG | PUB | — | — | PUBLIC_RAG + POLICY | KB `a2618c6d`; deterministic-INVOICE gate (fixed) | IMPLEMENTED_COMPLETE (fixed `45d3f00`) | `test_customer_rag_audit` | Y | — | Low | PHASE_6_REAL_LINE_UAT |
| CUS-G15 | Ai `1` r17 | ขอเบอร์ติดต่อ | Shipify 02-026-6426 / FT 02-026-6425 | RAG | PUB | — | — | PUBLIC_RAG | KB `service_information` | IMPLEMENTED_COMPLETE | `test_customer_rag_audit` | Y | — | — | NO_ACTION_REQUIRED |
| CUS-G16 | Ai `1` r18 | ร้านส่งหรือยังคะ | seller-dispatched? → asks purchase bill; answer from `shop_shipped_at` | ERP | PRIV | bill_no (PO/PA/POS/PE) | — | PRIVATE_ERP_READ | `decision_engine` PSI (`_SELLER_DISPATCH_RE` → order domain) → `searchdataorder`; `business_action_response_mapping` (status keywords widened `0937d71`) | IMPLEMENTED_PARTIAL — presents generic Order `Status`; **no `shop_shipped_at` field** | `test_customer_action1.test_g21_cancel_policy_stays_rag` (adjacent); scratchpad `test_g16g17` (uncommitted) | Y | `shop_shipped_at` not in `searchdataorder` response; same-message-PO edge routes to a read | Med | EXTERNAL_API_DEPENDENCY (field) + PHASE_5_DEFERRED_FIX (routing edge) |
| CUS-G17 | Ai `1` r19 | ติดตามสถานะ สินค้า | order status by bill → `tracking_no_th, carrier, status` | ERP | PRIV | bill_no | — | PRIVATE_ERP_READ | `decision_engine` PSI (tracking domain) → order/tracking read | IMPLEMENTED_PARTIAL — routes PRIVATE but **exact wording hits an ambiguity-clarification prompt**, not the source identifier-ask | scratchpad `test_g16g17` (uncommitted) | Y | multi-action tie for a generic "track goods status" phrase | Low | PHASE_5_DEFERRED_FIX |
| CUS-G18 | Ai `1` r20 | ยอดเงินไม่เข้า / เติมเงินแล้วรอตรวจสอบ | top-up not credited → ask slip; check wallet txns | ERP | PRIV | slip (+ optional amount/date via `_TOPUP_INLINE_RE`) | — | OPERATIONAL_WORKFLOW + PRIVATE_ERP_READ (`GET /wallet/.../transactions`) | `operational_change_flow.topup_not_credited` (collect + Human CS) | IMPLEMENTED_SAFE_FALLBACK — honest handoff; **no wallet-txn read wired**; inbound slip image not ingested (webhook text-only) | `test_customer_action1` (recognizer, turn-1); scratchpad `test_g18` (uncommitted) | Y | no wallet transactions API; no inbound-image ingest | Med | EXTERNAL_API_DEPENDENCY + PHASE_5 (attachment) |
| CUS-G19 | Ai `1` r21 | เรียกรถให้ได้ไหม / เหมารถ | fresh charter: confirm service (TC19) → collect bill + dest + receiver + phone → Human CS | HUMAN_CS | NA | shipment bill (FT/FE/SA/SP) + destination + receiver name + phone | 2 imgs | PUBLIC_RAG (TC19) + OPERATIONAL_WORKFLOW + HUMAN_CS | seeded TC19 KB chunk; `charter_truck_flow.py`; `decision_engine` charter branch (`97451f4` — bill-domain + decisive-intent break) | IMPLEMENTED_COMPLETE | `test_customer_rag2_charter_truck`, `test_customer_rag2_1_charter_slots`; scratchpad `test_g19` (uncommitted) | Y | evaluation-price still human (source expects it) | — | NO_ACTION_REQUIRED (staff price by design) |
| CUS-G20 | Ai `1` r22 | ขนส่งเอกชนมีอะไรบ้าง | private carriers: Nim/EMS/J&T/Flash + rate tables | RAG | PUB | — | 4 imgs | PUBLIC_RAG | KB | IMPLEMENTED_COMPLETE | `test_customer_rag_audit` | Y | rate-table images pending | Low | PHASE_3_RAG_COVERAGE |
| CUS-G21 | Ai `1` r23 | ยกเลิกบิลสั่งซื้อได้ไหม | cancel policy: unpaid → can cancel; paid → admin asks shop | ERP | PRIV | (policy answer needs none) | — | POLICY / RAG + HUMAN_CS | KB chunk `intent=สต็อก`; `test_g21_cancel_policy_stays_rag` | IMPLEMENTED_COMPLETE (policy) / NOT_SUPPORTED (execution) | `test_customer_action1.test_g21_cancel_policy_stays_rag`; scratchpad `test_g21` (uncommitted) | Y | no `CancelOrder` action; same-message-PO routes to read; KB wording keyed on "สั่งซื้อสำเร็จ" vs source "ชำระ/ยังไม่ชำระ" | Low-Med | EXTERNAL_API_DEPENDENCY + PHASE_5 (routing edge) + PHASE_3 (wording align) |
| CUS-G22 | Ai `1` r24 | เรทเท่าไหร่คะ | import + ฝากสั่ง rates (5.11 ฿/¥, road/sea) | RAG | PUB | — | 1 img | PUBLIC_RAG | KB rate FAQ | IMPLEMENTED_COMPLETE (wording fidelity → human judge) | `test_customer_rag_audit` | Y | wording fidelity | Low | PHASE_6_REAL_LINE_UAT |
| CUS-G23 | Ai `1` r25 | คูปองใช้ไม่หมด คืนได้ไหม | coupon: 1 bill/1 coupon, unused not refunded | RAG | PUB | — | — | PUBLIC_RAG + POLICY | KB `coupon_policy` | IMPLEMENTED_COMPLETE | `test_customer_rag_audit` | Y | — | — | NO_ACTION_REQUIRED |
| CUS-G24 | Ai `1` r26 | มีบริการอะไรบ้าง | services: ฝากสั่ง/ฝากนำเข้า/ฝากโอน | RAG | PUB | — | "check answer OK?" | PUBLIC_RAG | KB | IMPLEMENTED_COMPLETE (verbose — wording judge) | `test_customer_rag_audit` | Y | wording fidelity | Low | PHASE_6_REAL_LINE_UAT |
| CUS-G25 | Ai `1` r27 | ฝากสั่งกับฝากนำเข้าต่างกันอย่างไง | ฝากสั่ง (full service) vs ฝากนำเข้า (self-order) | RAG | PUB | — | 2 imgs | PUBLIC_RAG | KB | IMPLEMENTED_COMPLETE | `test_customer_rag_audit` | Y | image attach pending | Low | PHASE_3_RAG_COVERAGE |
| CUS-G26 | Ai `1` r28 | ชำระบิลขนส่งยังไง / **ชำระค่านำเข้ายังไง** | shipping-bill payment how-to (top-up + pay) | RAG | PUB | — | 2 imgs | PUBLIC_RAG | KB `06636112`; fuzzy-guard `ค่านำเข้า` (fixed) | IMPLEMENTED_COMPLETE (fixed `45d3f00`) | `test_customer_rag_audit` | Y | image attach pending | Low | PHASE_3_RAG_COVERAGE |
| CUS-G27 | Ai `1` r29 | สนใจฝากสั่ง ค่าใช้จ่ายคิดยังไง | 2-round cost structure | RAG | PUB | — | 2 imgs | PUBLIC_RAG | KB | IMPLEMENTED_COMPLETE | `test_customer_rag_audit` | Y | image attach pending | Low | PHASE_3_RAG_COVERAGE |
| CUS-G28 / CUS-F11 | Ai `1` r30 | มีบริการตีลังไม้ไหม | wood-crating service + conditions | RAG | PUB | — | 2 imgs | PUBLIC_RAG | KB | IMPLEMENTED_COMPLETE | `test_customer_rag_audit` | Y | image attach pending | Low | PHASE_3_RAG_COVERAGE |
| CUS-G29 / CUS-P20 / CUS-S20 | Ai `1` r31 / PDF p17 / Ai `2` r22-23 | ช่วยแปลงลิงก์ให้หน่อยค่ะ | 1688/Taobao/Tmall → shipify.co.th product link; **PUBLIC, NO auth** | WORKFLOW | PUB | product URL | 1 img | LINK_CONVERSION | `decision_engine` LINK_CONVERSION branch; `geturlproductdetail` action | IMPLEMENTED_COMPLETE (`CUSTOMER-LINK-1`, `-REAL-2`) | `test_customer_link1`, `test_customer_link_real2` | Y | — | — | NO_ACTION_REQUIRED |
| CUS-S01 | Ai `2` r4 (CSW1) | สินค้าจะเข้าไทยตอนไหนคะ | = G03 as a CS-workflow row: ask PO → check → forward to admin | ERP | PRIV | เลขบิลสั่งซื้อ (PO/PA/POS/PE) | — | PRIVATE_ERP_READ | as G03 | IMPLEMENTED_PARTIAL (no real ETA field) | `test_customer_erp_read1` | Y | no `estimated_arrival_th` | Med | EXTERNAL_API_DEPENDENCY |
| CUS-S02 | Ai `2` r5 (CSW2) | ต้องการแก้จำนวนสินค้าในบิล | change item qty in bill (WRITE) | ERP | PRIV | bill_no + item_id + new_qty | — | OPERATIONAL_WORKFLOW + BUSINESS_ACTION_WRITE | `operational_change_flow.modify_bill_qty` (collect PO → Human CS) | IMPLEMENTED_SAFE_FALLBACK — honest handoff; **no qty-modify WRITE API** | `test_customer_action1` (recognizer + turn1/turn2 E2E) | Y | no WRITE API | Med | EXTERNAL_API_DEPENDENCY |
| CUS-S03 | Ai `2` r6 (CSW3) | เปลี่ยนเป็นจัดส่งทางรถ/ทางเรือ | change shipping method (WRITE) | ERP | PRIV | bill_no + shipping_type | — | OPERATIONAL_WORKFLOW + WRITE | `operational_change_flow.change_shipping_method` (collect bill + method → Human CS) | IMPLEMENTED_SAFE_FALLBACK | `test_customer_action1`; scratchpad `test_csw3_real1_e2e` (uncommitted) | Y | no WRITE API | Med | EXTERNAL_API_DEPENDENCY |
| CUS-S04 | Ai `2` r7 (CSW4) | ลืมเลือก VAT / ต้องการ VAT | add VAT flag to purchase bill | ERP | PRIV | เลขบิลสั่งซื้อ | — | OPERATIONAL_WORKFLOW + WRITE | `operational_change_flow.add_vat` (collect PO → Human CS) | IMPLEMENTED_SAFE_FALLBACK | `test_customer_action1`; scratchpad `test_csw4_e2e` (uncommitted) | Y | no WRITE API; stage-2 is human-filled amount | Med | EXTERNAL_API_DEPENDENCY |
| CUS-S05 | Ai `2` r8 (CSW5) | ถอนเงินสั่งซื้อยังไง | purchase-credit withdrawal how-to (app → เครดิตสั่งซื้อ → yellow button → bank → 3-5d) | ERP→RAG | PRIV | customer_id (from binding) | — | PUBLIC_RAG (how-to) | `withdrawal_flow.purchase_withdrawal_reply` → KB `PURCHASE_WITHDRAWAL` | IMPLEMENTED_COMPLETE | scratchpad `test_pb_step456` (uncommitted); `test_sem1_private_state_routing` | Y | route label ERP vs actual RAG (stale expectation) | Low | NO_ACTION_REQUIRED (behaviour correct) |
| CUS-S06 | Ai `2` r9 (CSW6) | สั่งผลิตตามสเปค / สกรีนโลโก้ | custom production: collect bill + spec + qty + colour + logo → 3rd party | HUMAN_CS | NA | เลขบิลสั่งซื้อ, สเปค, จำนวน, สี, โลโก้ | — | OPERATIONAL_WORKFLOW + HUMAN_CS | `operational_change_flow.custom_production` | IMPLEMENTED_COMPLETE (Human CS by design) | `test_customer_action1` (recognizer) | Y | — | — | NO_ACTION_REQUIRED |
| CUS-S07 | Ai `2` r10 (CSW7) | โหลดใบกำกับยังไง / ขอใบกำกับค่าสินค้า | invoice-download instructions (website steps) | ERP | PRIV | (bill = condition only) | — | PUBLIC_RAG (instructions) | KB `intent=บัญชี` chunk; INVOICE family → RAG | IMPLEMENTED_COMPLETE (instructions) / NOT_SUPPORTED (fetch) | `test_customer_invoice1`; scratchpad `test_csw7` (uncommitted) | Y | no invoice PDF/URL API; same-message-PO routes to `searchdataorder`; route label ERP vs the F10 twin (RAG) | Low-Med | EXTERNAL_API_DEPENDENCY + PHASE_5 (routing edge) + CUSTOMER_CLARIFICATION (route label) |
| CUS-S08 | Ai `2` r11 (CSW8) | บิลขนส่งนี้/แทรคนี้เป็นของบิลสั่งซื้อไหน | reverse-map shipment bill / CN tracking → purchase bill (+ETA) | ERP | PRIV | tracking_cn OR bill_no (FT/FE/SA/SP) | — | OPERATIONAL_WORKFLOW + PRIVATE_ERP_READ ("Read API — map tracking → บิลสั่งซื้อ") | `operational_change_flow.map_shipment_to_purchase_bill` (`e639a5b` — collect + Human CS) | IMPLEMENTED_SAFE_FALLBACK — honest Human CS; **no reverse-map field** (`searchdatashipment`/`searchdatatracking` return no `OrderCode`) | `test_customer_action1` (recognizer); scratchpad `test_csw8` (uncommitted) | Y | no `purchase_bill_no` in shipment/tracking response | Med | EXTERNAL_API_DEPENDENCY |
| CUS-S09 | Ai `2` r12 (CSW9) | บิลขนส่ง FTxxx เปลี่ยนที่อยู่จัดส่ง | change delivery address | ERP | PRIV | shipment bill (FT/FE/SA/SP) + new address + receiver + phone | — | BUSINESS_ACTION_WRITE | `requestshippingaddresschange` action (enabled) + `decision_engine` dynamic collection | IMPLEMENTED_COMPLETE (the ONE wired WRITE; honest submission reply) | `test_customer_action1.TestRoutingE2E.test_address_change_S09_keeps_its_own_business_action`; `test_p0_01_*` | Y | — | — | NO_ACTION_REQUIRED |
| CUS-S10 | Ai `2` r13 (CSW10) | รีเเพ็คค่ะ | repack/consolidate multi-bill at TH warehouse | HUMAN_CS | NA | shipment_bill_no_list | — | OPERATIONAL_WORKFLOW + HUMAN_CS | `operational_change_flow.repack` (`ff661d0` — multi-bill collect → Human CS) | IMPLEMENTED_COMPLETE (Human CS by design) | `test_customer_action1` (recognizer); scratchpad `test_csw10` (uncommitted) | Y | — | — | NO_ACTION_REQUIRED |
| CUS-S11 | Ai `2` r14 (CSW11) | FT เปลี่ยนเป็นรับเอง / ส่งเอกชน | change carrier / self-pickup (WRITE) | ERP | PRIV | shipment_bill_no + new_carrier/type | — | OPERATIONAL_WORKFLOW + WRITE | `operational_change_flow.change_carrier_or_selfpickup` (`56dba99` — collect bill + target → Human CS) | IMPLEMENTED_SAFE_FALLBACK | `test_customer_action1` (recognizer); scratchpad `test_csw11` (uncommitted) | Y | no WRITE API | Med | EXTERNAL_API_DEPENDENCY |
| CUS-S12 | Ai `2` r15 (CSW12) | ถอนเงินขนส่งยังไงคะ | shipping withdrawal — **brand-conditional** SP (form img + ID copy) vs FT (menu → button); **SP answer needs image attached** | ERP→RAG | PRIV | brand (from CustCode) or asked | **SP form image (PDF pg14)** | PUBLIC_RAG (brand-branched) + HUMAN_CS (SP docs) | `withdrawal_flow.py` (`31dec02` — SP/FT + brand follow-up) → KB `SHIPPING_WITHDRAWAL_SP`/`_FT` | IMPLEMENTED_COMPLETE (text) — **SP form image NOT attached** | scratchpad `test_csw12` (uncommitted) | Y | SP withdrawal-form image not sent; route label ERP vs actual RAG | Low-Med | PHASE_3_RAG_COVERAGE (image) + CUSTOMER_CLARIFICATION (route) |
| CUS-S13 | Ai `2` r16 (CSW13) | บิลซ้ำค่ะ | duplicate bill — check CN tracking, delete extras | ERP | PRIV | tracking_cn | — | OPERATIONAL_WORKFLOW + **Write+Delete API (⚠ human confirm)** | `operational_change_flow.duplicate_bill` (`4cebf85` — collect CN tracking → Human CS) | IMPLEMENTED_SAFE_FALLBACK | `test_customer_action1` (`test_cn_tracking_all_digits...`); scratchpad `test_csw13` (uncommitted) | Y | no delete API (source itself flags "⚠ may need human confirm") | Med | EXTERNAL_API_DEPENDENCY |
| CUS-S14 | Ai `2` r17 (CSW14) | ใช้คูปองยังไง | coupon usage how-to (5 steps) | RAG | PUB | — | — | PUBLIC_RAG | KB `coupon_policy` | IMPLEMENTED_COMPLETE | `test_customer_rag_audit` | Y | — | — | NO_ACTION_REQUIRED |
| CUS-S15 | Ai `2` r18 (CSW15) | ใส่ที่อยู่โกดังจีนถูกไหมคะ | verify entered CN warehouse address ("Read API — validate") | ERP | PRIV | customer_warehouse_address + customer_id | — | PRIVATE_ERP_READ + HUMAN_CS | `operational_change_flow.verify_warehouse_address` (`89d12cf` — collect address + binding CustCode → Human CS) | IMPLEMENTED_SAFE_FALLBACK — honest Human CS; **no validate API** | `test_customer_action1` (recognizer); scratchpad `test_csw15` (uncommitted) | Y | no address-validate API | Med | EXTERNAL_API_DEPENDENCY |
| CUS-S16 | Ai `2` r19 (CSW16) | รวมบิลเหมารถค่ะ | combine bills into one charter truck (Human CS) | HUMAN_CS | PRIV | shipment_bill_no_list | — | OPERATIONAL_WORKFLOW + HUMAN_CS | `operational_change_flow.combine_bills_charter` | IMPLEMENTED_COMPLETE (Human CS by design) | `test_customer_action1` (recognizer); scratchpad `test_purchbill`/`test_pb_step456` (uncommitted) | Y | — | — | NO_ACTION_REQUIRED |
| CUS-S17 | Ai `2` r20 (CSW17) | ขอแทรคไทยค่ะ (ลูกค้าไม่เข้าเช็คเอง) | Thai-tracking retrieval by shipment bill + status link; **ask tracking BEFORE saying "no"** | ERP | PRIV | shipment_bill_no | — | PRIVATE_ERP_READ | `decision_engine` CUSTOMER-TRACK-TH-1 branch → `searchdatashipment` `TrackingTH` | IMPLEMENTED_COMPLETE | `test_customer_track_th1`, `test_customer_track_th1_1` | Y | status link + carrier text partial | Low | PHASE_6_REAL_LINE_UAT |
| CUS-S18 | Ai `2` r21 (CSW18) | วันนี้มีของเข้าไทยไหมคะ | daily arrivals — customer-scoped list; "today" filter | ERP | PRIV | customer_id | — | PRIVATE_ERP_READ | `decision_engine` PSI LIST_ALL → `searchdatashipmentlist` | IMPLEMENTED_PARTIAL — routes + customer-scoped; **collapses to latest-record Status; empty list → generic "ดำเนินการเรียบร้อยค่ะ"; no per-record arrival DATE for a "today" filter** | scratchpad `test_csw18` (uncommitted) | Y | answer-shape + zero-result wording + no arrival-date field | Med | PHASE_5_DEFERRED_FIX + EXTERNAL_API_DEPENDENCY (date field) |
| CUS-S20 (a) | Ai `2` r22 | ตัวอย่างแปลงลิงก์ | link-conversion example (raw links) | NA | NA | — | 1 img | LINK_CONVERSION | as G29 | IMPLEMENTED_COMPLETE | `test_customer_link1` | Y | duplicate case_id | Low | PHASE_5_DEFERRED_FIX (data hygiene — renumber) |
| CUS-S20 (b) | Ai `2` r23 | ช่วยแปลงลิงก์ให้หน่อยค่ะ | link-conversion request + output | NA | NA | product URL | 1 img | LINK_CONVERSION | as G29 | IMPLEMENTED_COMPLETE | `test_customer_link1`, `test_customer_link_real2` | Y | — | — | NO_ACTION_REQUIRED |
| CUS-F01 | ปัญหา r2 | ครีมอาบน้ำนำเข้าได้ไหม | classify "ครีมอาบน้ำ = ของเหลว = can't import" + ask other products | RAG | PUB | — | — | PUBLIC_RAG + reasoning | KB prohibited-goods | IMPLEMENTED_COMPLETE (verdict PASS) | `test_customer_rag_audit`, `test_evidence_classifier` | Y | reasoning-over-list is fragile | Low | PHASE_6_REAL_LINE_UAT |
| CUS-F02 | ปัญหา r3 | ทางรถกับทางเรือระยะเวลากี่วัน | answer ONLY the asked one (road), not sea | RAG | PUB | — | — | PUBLIC_RAG | KB transit FAQ | IMPLEMENTED_COMPLETE (verdict PASS) | `test_customer_rag_audit` | Y | customer note contradicts the question shape | Low | PHASE_6_REAL_LINE_UAT |
| CUS-F03 | ปัญหา r4 | แล้วเรทนำเข้าเท่าไหร่คะ | rate answer WITHOUT the "เผื่อ 3-5 วันด่านเวียดนาม" clause | RAG | PUB | — | — | PUBLIC_RAG | KB rate FAQ | IMPLEMENTED_PARTIAL — the transient-clause removal is a **content edit the customer requested** | `test_customer_rag_audit` | Y | KB still carries the transient clause | Low | PHASE_3_RAG_COVERAGE |
| CUS-F04 | ปัญหา r5 | ฝากสั่งน้ำหอมได้ไหมคะ | "น้ำหอม = ของเหลว = can't import" | RAG | PUB | — | — | PUBLIC_RAG + reasoning | KB prohibited-goods | IMPLEMENTED_COMPLETE (verdict PASS) | `test_customer_rag_audit` | Y | reasoning-over-list | Low | PHASE_6_REAL_LINE_UAT |
| CUS-F05 | ปัญหา r6 | สั่งแบตเตอรี่จำนวนเยอะได้ไหม | prohibited (battery) — **recorded FAIL** on the "large quantity" framing | RAG | PUB | — | — | PUBLIC_RAG + reasoning | KB prohibited-goods | **IMPLEMENTED_PARTIAL — recorded FAIL** ("แก้ไขคำตอบอีกที") | `test_customer_rag_audit` | Y | "จำนวนเยอะ" framing pulls a rate answer | Med | PHASE_5_DEFERRED_FIX |
| CUS-F06 | ปัญหา r7 | โกดังอ่อนนุชเปิดทุกวันหรอคะ | Mon-Fri 9-18, closed Sat-Sun | RAG | PUB | — | — | PUBLIC_RAG | KB warehouse hours | IMPLEMENTED_COMPLETE (verdict PASS) | `test_customer_rag1_pickup_location` | Y | — | — | NO_ACTION_REQUIRED |
| CUS-F07 | ปัญหา r8 | มีขนส่งทางเครื่องบินไหม | road & sea only; no air; ask which type | RAG | PUB | — | — | PUBLIC_RAG | KB | IMPLEMENTED_COMPLETE (verdict PASS) | `test_customer_rag_audit` | Y | — | — | NO_ACTION_REQUIRED |
| CUS-F08 | ปัญหา r9 | ใบกำกับค่าสินค้าออกได้ไหม | = G05 — **PASS 50% / FAIL 50% / incomplete loop** | RAG | PUB | product type + customer type + import mode | — | PUBLIC_RAG (multi-turn slot) | INVOICE family; KB | IMPLEMENTED_PARTIAL — loop not closed | `test_customer_invoice1`, `test_invoice_product_regression2` | Y | multi-turn slot-fill for invoice eligibility | Med | PHASE_5_DEFERRED_FIX |
| CUS-F09 | ปัญหา r10 | จัดส่งสินค้าถึงหน้าบ้านเลยไหม | yes, door-to-door; choose carrier | RAG | PUB | — | — | PUBLIC_RAG | KB | IMPLEMENTED_COMPLETE (verdict PASS) | `test_customer_rag_audit` | Y | — | — | NO_ACTION_REQUIRED |
| CUS-F10 | ปัญหา r11 | โหลดใบกำกับยังไง | = S07 instructions; **verdict PASS** (KB chunk added) | RAG | PUB | — | — | PUBLIC_RAG | KB `intent=บัญชี` chunk | IMPLEMENTED_COMPLETE | `test_customer_invoice1`; scratchpad `test_csw7` | Y | retrieval confidence (historical "ไม่มีข้อมูล") | Low | PHASE_6_REAL_LINE_UAT |
| CUS-F11 | ปัญหา r12 | ตีลังไม้ได้ไหม | = G28 — crating service + conditions | RAG | PUB | — | — | PUBLIC_RAG | KB | IMPLEMENTED_COMPLETE (verdict PASS) | `test_customer_rag_audit` | Y | image attach pending | Low | PHASE_3_RAG_COVERAGE |
| CUS-SC1 | screenshot | มีบริการเหมารถไหมคะ | confirm charter service exists (was: wrongly "no info") | RAG | PUB | — | — | PUBLIC_RAG (TC19) | seeded TC19 KB chunk | IMPLEMENTED_COMPLETE (Phase 1 G19/SC1) | `test_customer_rag2_charter_truck` | Y | — | — | NO_ACTION_REQUIRED |
| CUS-SC2 | screenshot | (identity-verification loop) | self-service verification must NOT loop; on mismatch → clean handoff, don't re-loop on next question | ERP | PRIV | — | — | AUTH_CUSTOMER_BINDING | `services/customer_binding_service.py` / verification flow (NOT audited in Phase 1) | **UNKNOWN — NOT_IMPLEMENTED / not verified** | — | Y | verification-loop defect; desired behaviour under-specified in master | **High** | PHASE_5_DEFERRED_FIX (+ CUSTOMER_CLARIFICATION for desired flow) |
| CUS-SC3 | screenshot | 54x12x43 (dims reply to a calc) | after weight+dims for a PUBLIC calc, must NOT ask for the registered phone | CLARIFY | PUB | weight + dims | — | CALCULATOR | `decision_engine` calculator flow | IMPLEMENTED_COMPLETE (calc multi-turn; PPC boundary) | `test_customer_calc11_conversation_state`, `test_customer_uat_fix1_public_private_boundary`, `ppc_boundary_eval` | Y | — | Low | PHASE_6_REAL_LINE_UAT |
| CUS-P06 | PDF p6 | (company fact genuinely not in KB) | genuine no-info → "ขอเช็กข้อมูลเพิ่มเติมให้ก่อนนะคะ" + real `sendlinenotics` handoff | HUMAN_CS | PUB | — | — | HUMAN_CS | `decision_engine` Fix-2 unsupported-company-fact handoff | IMPLEMENTED_COMPLETE | `test_customer_uat_fix2_unsupported_fact_handoff` | Y | forward channel ("another LINE group?") unspecified | Low | CUSTOMER_CLARIFICATION (channel) |
| CUS-P07 | PDF p5/p7 | คิดค่านำเข้าให้หน่อย / คำนวนค่าขนส่ง | calc; when not computable send `https://www.shipify.co.th/Rate` | RAG | PUB | weight + dims (or link) | — | CALCULATOR + RAG | calculator flow + rate link | IMPLEMENTED_COMPLETE | `test_customer_calc1`, `test_calculator_regression_2` | Y | — | Low | PHASE_6_REAL_LINE_UAT |
| CUS-P20 | PDF p17 + Ai `1` #29 | ช่วยแปลงลิงก์ให้หน่อยค่ะ | = G29 link conversion, PUBLIC, **no security/identity** | WORKFLOW | PUB | product URL | — | LINK_CONVERSION | as G29 | IMPLEMENTED_COMPLETE | `test_customer_link1` | Y | — | — | NO_ACTION_REQUIRED |
| CUS-RL-* (×3) | `tests/fixtures/real_line/*.json` | (replay fixtures) | internal state/replay regression fixtures — NOT customer feedback | — | — | — | — | OTHER | `test_p0_01_*`, `test_known_real_line_cases` | IMPLEMENTED_COMPLETE | those tests | — | — | — | NO_ACTION_REQUIRED |

---

## 5. Missing Requirements (source item → not represented / under-represented in master)

| # | Requirement (source) | Where in source | Master status | Classification | Next phase |
|---|---|---|---|---|---|
| M1 | **Cheapest private-carrier comparison by CustCode** ("check shipping cost in the system by customer code — which is cheapest, Kerry/JT/Nim/EMS") | PDF page 2 item 3; echoed by `searchdatashipmentlist.search_keywords` (`ถูกที่สุด`,`ถูกกว่า`) | **MISSING_FROM_MASTER** — no dedicated `case_id` | PRIVATE_ERP_READ (compare) | EXTERNAL_API_DEPENDENCY (needs a per-CustCode carrier-cost compare) + PHASE_5 (routing intent) |
| M2 | **CSW12 SP answer must attach the withdrawal-form image** | PDF page 14 ("*พร้อมมีรูปการถอนเงินแนบไปให้ดูด้วย") | `CUS-S12` present but the **image-attachment requirement is not in master fields** | PUBLIC_RAG + media | PHASE_3_RAG_COVERAGE |
| M3 | **G09 payment how-to must NOT include the claims/เคลม content** (prohibited-answer rule) | PDF page 8 (TC9) | not captured as an `expected_answer_constraints` entry (that field is empty on every row) | POLICY / prohibited-answer | PHASE_3_RAG_COVERAGE |
| M4 | **Attachment/image delivery for the ~13 "answer OK, image pending" cases** (G01,G02,G06,G09,G20,G22,G25,G26,G27,G28, CN-warehouse imgs, etc.) | `Ai.xlsx` `1` `สื่อรูปภาพ` column + the 13-tally | `media_refs` present on 12 rows, but **the platform does not send KB images for these FAQ answers** | PUBLIC_RAG + media | PHASE_3_RAG_COVERAGE |
| M5 | **F03 rate answer: remove the transient "เผื่อ 3-5 วัน ด่านเวียดนาม" clause** | ปัญหา r4 note "แก้ไขข้อมูลเดิม เอา … ออก" | `CUS-F03` present; the content edit is not applied to the KB chunk | PUBLIC_RAG content edit | PHASE_3_RAG_COVERAGE |
| M6 | **`expected_answer_constraints` field is empty on all 69 rows** — every "must / must not" reviewer rule (per-PDF-page) has no machine-readable home | PDF pages 5–17 | structural gap | — | PHASE_5_DEFERRED_FIX (master hygiene) |

---

## 6. Partial Requirements

| Case | Why partial | Next phase |
|---|---|---|
| CUS-G03 / CUS-S01 | ETA answered from generic `Status`, no `estimated_arrival_th` field | EXTERNAL_API_DEPENDENCY |
| CUS-G05 / CUS-F08 | invoice-issuance multi-turn loop not closed (ask product → confirm can issue) | PHASE_5_DEFERRED_FIX |
| CUS-G08 (+F01/F04/F05) | prohibited-goods sub-category reasoning fragile; F05 recorded FAIL | PHASE_5_DEFERRED_FIX + PHASE_3 |
| CUS-G16 | generic Order `Status`, no `shop_shipped_at` | EXTERNAL_API_DEPENDENCY |
| CUS-G17 | exact wording → ambiguity-clarification not identifier-ask | PHASE_5_DEFERRED_FIX |
| CUS-G18 | no wallet-txn read; no inbound slip-image ingest | EXTERNAL_API_DEPENDENCY + PHASE_5 |
| CUS-S02/S03/S04/S11/S13/S15 | operational WRITE / delete / validate APIs not wired — honest Human-CS fallback | EXTERNAL_API_DEPENDENCY |
| CUS-S08 | reverse-map field absent | EXTERNAL_API_DEPENDENCY |
| CUS-S12 | SP form image not attached | PHASE_3_RAG_COVERAGE |
| CUS-S18 | answer-shape collapse + zero-result wording + no arrival-date | PHASE_5_DEFERRED_FIX + EXTERNAL_API_DEPENDENCY |
| CUS-SC2 | verification-loop defect; behaviour not verified | PHASE_5_DEFERRED_FIX |
| CUS-F03 | transient clause still in KB | PHASE_3_RAG_COVERAGE |

---

## 7. RAG Coverage Gaps

| # | Gap | Evidence | Next phase |
|---|---|---|---|
| R1 | **KB images not delivered** for ~13 "answer OK, image pending" FAQ rows (`สื่อรูปภาพ` column) | `Ai.xlsx` `1` tally; `media_refs` on 12 master rows | PHASE_3_RAG_COVERAGE |
| R2 | **F03 rate chunk still carries the transient "ด่านเวียดนาม 3-5 วัน" clause** the customer asked to remove | ปัญหา r4 | PHASE_3_RAG_COVERAGE |
| R3 | **Invoice-issuance multi-turn eligibility loop** (ask product type → customer type → import mode → confirm) not represented as KB/flow | PDF p7 item 10; F08 verdict | PHASE_5_DEFERRED_FIX (flow) |
| R4 | **G09 "no claims content" prohibited-answer rule** unrepresented | PDF p8 TC9 | PHASE_3_RAG_COVERAGE |
| R5 | **G21 cancel-policy chunk wording** keyed on status "สั่งซื้อสำเร็จ"; source frames it "ชำระ / ยังไม่ชำระ" — semantically consistent, wording drift | KB `intent=สต็อก` vs CUS-G21 answer | PHASE_3_RAG_COVERAGE |
| R6 | **Duplicate/stale invoice chunk `546c1bd5`** (omits bill/payment condition, taxpayer-info, credit-card limit) — routed around deterministically, not deleted | `CUSTOMER_RAG_AUDIT.md` §Duplicate | PHASE_3_RAG_COVERAGE (dedup decision) |

No public/private mismatch found in the RAG subset beyond the S12/S07 route-label
drift (§10). No source that says RAG-but-implemented-as-ERP. Two sources say
ERP but the behaviour is (correctly) RAG: **S05, S12, S07** (how-to content) —
route-label drift, not a behaviour bug (§10).

---

## 8. ERP / Business Action Capability Gaps  (all EXTERNAL_API_DEPENDENCY)

Live enabled-action set (verified read-only this phase): `getdatacustomer`,
`customer_data_lookup`, `search_data_order`, `searchdataorder`,
`searchdataorderlist`, `search_po`, `searchdatashipment`,
`searchdatashipmentlist`, `searchdatatracking`, `geturlproductdetail`,
`sendlinenotics`, `requestshippingaddresschange`. Everything else in the
registry is a mock / disabled fixture (`fixture_cancel_order` = `enabled:false`,
Mock*Provider = mock).

The customer's own `Ai.xlsx` `API ที่ต้องขอ` / `API Output ที่คาดหวัง` columns
are a **request list to their ERP team** — the current FastTrade endpoints are
READ-only status lookups. Gaps:

| # | Case(s) | Expected capability (source `API Output`) | Enabled action today | Verdict |
|---|---|---|---|---|
| E1 | G03, S01 | `GET /orders/{bill_no}/tracking → estimated_arrival_th, status` | `searchdataorder` / `searchdatashipment` → `Status` only | **NOT SUPPORTED** — no ETA-to-TH field |
| E2 | G11 | `GET /orders/{bill_no}/items` + `POST /claims {bill_no, tracking_cn, evidence}` | none | **NOT SUPPORTED** — no items read, no claims WRITE |
| E3 | G16 | `GET /orders/{bill_no}/status → shop_shipped_at, status` | `searchdataorder` → `Status` only | **NOT SUPPORTED** — no `shop_shipped_at` (seller-dispatch) field |
| E4 | G18 | `GET /wallet/{customer_id}/transactions` (filter date/amount) | none | **NOT SUPPORTED** — no wallet-transaction read |
| E5 | G21 | `GET /orders/{bill_no}/status → payment_status` + an executable cancel | `searchdataorder` (`Status` only); `fixture_cancel_order` disabled | **PARTIALLY** (status readable) / **NOT SUPPORTED** (cancel execution + `payment_status` semantics) |
| E6 | S02, S03, S04, S11 | order-modify WRITE (`bill_no + item_id + new_qty` / `shipping_type` / VAT flag / `new_carrier`) → `success/fail` | none (only `requestshippingaddresschange` is a wired WRITE) | **NOT SUPPORTED** — no order-modify WRITE APIs |
| E7 | S08 | Read API — `tracking_cn` OR `bill_no` → `purchase_bill_no, shipment_bill_no, ETA` | `searchdatashipment`/`searchdatatracking` → `Code/Status/TrackingCH/TrackingTH/TotalSum` (no `OrderCode`) | **NOT SUPPORTED** — no shipment/tracking → purchase-bill reverse map |
| E8 | S13 | `tracking_cn → duplicate_bills_list, delete_confirmation` (⚠ human confirm) | none | **NOT SUPPORTED** — no duplicate-list read, no delete WRITE |
| E9 | S15 | `customer_warehouse_address + customer_id → is_valid, correct_address` | none | **NOT SUPPORTED** — no address-validate read |
| E10 | S18 | per-record `DateArrivedTH` / "rows awaiting TH confirmation" to answer a genuine "today" filter | `searchdatashipmentlist.$.data.0.DateArrivedTH` (latest-only) | **PARTIALLY** — a latest-record `DateArrivedTH` field exists on the LIST mapping but is not used to build a today-scoped answer |
| E11 | M1 (missing case) | per-CustCode private-carrier cost compare (cheapest of Nim/EMS/J&T/Flash) | none | **NOT SUPPORTED** |

**Re-verified Phase-1 gaps (still true at `0937d71`):**
* **CSW8 reverse map** → E7 — NOT SUPPORTED.
* **G16 seller-dispatch field** → E3 — NOT SUPPORTED.
* **CSW7 invoice PDF/URL** → no action; instructions-only KB — NOT SUPPORTED.
* **G21 CancelOrder** → E5 — NOT SUPPORTED (`fixture_cancel_order` disabled).

**Workflow gaps** (source workflow not in the known Phase-1 list): **only M1**
(cheapest-carrier compare). Everything else in `Ai.xlsx` `2.ต้องเช็คในระบบ`
(CSW1–CSW18, CSW20) has an implemented flow or safe fallback. The
"member vs general calc" split (PDF p7 item 9) is already handled by the
existing calculator + PPC boundary.

---

## 9. Comments / Notes as Requirements

| Source note | Introduces | Mapped? | Where |
|---|---|---|---|
| `Ai.xlsx` `1` r5 `ตอบไม่ครบลูปต้องแก้ต่อ` (G03) | incomplete-loop rule | partly (S01 workflow) | E1 |
| `Ai.xlsx` `1` `API ที่ต้องขอ` (every ERP row) | required-API list | **NOT in master fields** | §8 (captured here) |
| `Ai.xlsx` `2` `API Output ที่คาดหวัง` (every CSW row) | expected trusted fields | **NOT in master fields** | §8 |
| `Ai.xlsx` `2` r16 (CSW13) `⚠️ Write+Delete API — ต้องระวัง อาจต้อง human confirm` | destructive-action confirmation rule | partly (Human CS fallback) | E8 |
| PDF p2 item 3 | cheapest-carrier compare | **NO** | M1 |
| PDF p5 item 7 / p7 item 9 | send `/Rate` link; member-vs-general calc; no identity for general calc | yes (P07, SC3) | matrix |
| PDF p7 item 10 | invoice multi-turn loop | partly (G05) | R3 |
| PDF p8 TC9 | no claims content on payment how-to | **NO** | M3/R4 |
| PDF p14 (CSW12) | SP answer + withdrawal-form image | partly (S12 text only) | M2 |
| PDF p15 (CSW17) | ask tracking BEFORE "no data" | yes (implemented) | matrix (S17) |
| PDF p16 (CSW18) | "no shipment record" must be derivable when never ordered | partly | E10 / S18 deferred |
| PDF p17 (CSW20) | link conversion = PUBLIC, remove security/identity | yes (implemented) | matrix (G29/P20) |
| screenshot SC2 | verification must not loop | partly (NEEDS_INTERPRETATION) | SC2 High |
| `ปัญหา` r6 (F05) | large-quantity framing must not pull a rate answer | recorded FAIL | matrix (F05) |
| `ปัญหา` r4 (F03) | remove transient clause | yes (F03) | M5/R2 |

---

## 10. Duplicate / Conflict Report

### Duplicates
| Item | Classification | Action |
|---|---|---|
| `CUS-S20` ×2 (r22 `ตัวอย่างแปลงลิงก์` + r23 `ช่วยแปลงลิงก์…`) | **SAME_INTENT_DIFFERENT_FACET** of CSW20 Link Conversion (raw-link example vs conversion request+output). Shared `case_id` is a data-hygiene defect. | **RECORD ONLY.** Renumber to `CUS-S20a/CUS-S20b` in a later master-hygiene pass → PHASE_5_DEFERRED_FIX. Do not merge/delete this phase. |
| `CUS-G29` ≡ `CUS-P20` ≡ `CUS-S20(b)` | **LINKED_CASE / VARIANT** — one requirement (Link Conversion) across 3 sources. `linked_cases` present. | keep as-is; all PASS. |
| `CUS-F10` ≡ `CUS-S07` (`โหลดใบกำกับยังไง`) | **VARIANT** — same wording, F10 verdict PASS. | keep; see conflict C1. |
| `CUS-F08` ≡ `CUS-G05` (`ออกใบกำกับ…ได้ไหม`) | **VARIANT** — same intent. | keep; both flag the incomplete loop. |
| `CUS-G03` ≈ `CUS-S01` (`สินค้าจะเข้าไทยตอนไหน`) | **SAME_INTENT_DIFFERENT_REQUIREMENT** — G03 is the FAQ-catalogue framing, S01 the CS-workflow framing (step: ask PO → check → forward). | keep both. |

### Conflicts
| # | Source A | Source B | Conflict | Newer/authoritative? | Clarification needed? |
|---|---|---|---|---|---|
| C1 | `CUS-S07` `expected_route: ERP` / PRIVATE (`Ai.xlsx` `2` CSW7) | `CUS-F10` `expected_route: RAG` / PUBLIC (`ปัญหา` r11, verdict PASS) — identical answer | Same question, different route+privacy label | F10 is the **2/9/2025 real UAT run** (more recent) and marks the RAG behaviour PASS. The `Ai.xlsx` "ERP/PRIVATE" tag reflects the *bill-condition* note, not a bill lookup. | **YES** — confirm CSW7 is PUBLIC RAG instructions (as F10) vs PRIVATE ERP. → CUSTOMER_CLARIFICATION |
| C2 | `CUS-S12` `expected_route: ERP` / PRIVATE (`Ai.xlsx` `2` CSW12) | Behaviour + `CUSTOMER_MASTER_FAILURE_INVENTORY.md` classify it **STALE_EXPECTATION** — same how-to family as S05/S07, should read RAG | Route label vs actual (correct) RAG how-to behaviour | The failure-inventory (post-baseline) says the label is stale. | **YES** — confirm the S05/S07/S12 withdrawal/invoice how-tos are RAG (with a private brand-branch for S12). → CUSTOMER_CLARIFICATION |
| C3 | `CUS-P06` desired: forward to "another LINE group?" (PDF p6) | Implementation: `sendlinenotics` to the configured CS channel | Forward channel unspecified in source | — | **YES** — confirm the handoff target channel. → CUSTOMER_CLARIFICATION |

No same-question-different-answer or same-workflow-different-documents
conflict found beyond route-label drift.

---

## 11. Test Coverage

| Bucket | Coverage | Notes |
|---|---|---|
| RAG / public (33) | **TESTED_INDIRECTLY** via `test_customer_rag_audit`, `test_customer_rag1_pickup_location`, `test_answer_planner`, `test_evidence_classifier`, `test_invoice_product_regression2` | live-retrieval quality is REAL_LINE_ONLY |
| Calculator (G04/P07/SC3) | **TESTED_FOCUSED** — `test_customer_calc1`, `test_customer_calc11_conversation_state`, `test_excel_calculator`, `test_calculator_regression_2`, `ppc_boundary_eval` | strong |
| Link conversion (G29/P20/S20) | **TESTED_FOCUSED** — `test_customer_link1`, `test_customer_link_real2` | strong |
| Charter (G19/SC1, S16) | **TESTED_FOCUSED** — `test_customer_rag2_charter_truck`, `test_customer_rag2_1_charter_slots` | strong |
| Thai Tracking / Shipment status (S17, G12, G03) | **TESTED_FOCUSED** — `test_customer_track_th1`, `test_customer_track_th1_1`, `test_customer_erp_read1` | strong |
| Operational kinds (S02,S03,S04,S06,S08,S10,S11,S13,S15,S16, G11) | **TEST_EXISTS_BUT_WEAK** — `test_customer_action1` covers the *recognizer* + a few turn-1/turn-2 E2E; the Phase-1 per-workflow focused suites (`test_csw*.py`, `test_g*.py`) are **scratchpad-only, NOT committed** | **GAP** |
| CSW9 address change (S09) | **TESTED_FOCUSED** — `test_customer_action1.TestRoutingE2E`, `test_p0_01_*` | strong |
| Withdrawals (S05, S12) | **TEST_EXISTS_BUT_WEAK** — `test_sem1_private_state_routing` touches routing; the SP/FT brand-branch E2E is scratchpad-only | **GAP** |
| G16/G17/G18/G21/CSW18 | **NO committed focused test** — only scratchpad `test_g16g17.py`, `test_g18.py`, `test_g21.py`, `test_csw18.py` (uncommitted) | **GAP** |
| SC2 verification loop | **NO_TEST** | **GAP (High)** |
| Cross-flow / precedence / state | **TESTED_FOCUSED** — `test_cross_flow_matrix`, `test_known_real_line_cases`, `test_system_state_emergency_1`, `test_semantic_first_2*` | strong |

**Top test gap:** the Phase-1 workflow focused tests were authored as scratchpad
files and never committed. They should be promoted into `tests/` (adapted off
network-dependent mocks). → PHASE_5_DEFERRED_FIX (test hardening).

---

## 12. Deferred Fix Backlog (consolidated, deduplicated)

| # | Case | Actual | Expected | Missing API / semantic | Component | Severity | Next phase |
|---|---|---|---|---|---|---|---|
| D1 | CUS-S18 | daily-arrivals answer collapses to latest-record `Status`; multi-row not summarised | arrival-oriented compact list from trusted `Status` filter | — | `decision_engine._compose_natural_reply` / `select_requested_mapped_fields` / `_detect_aggregation_request` | Med | PHASE_5_DEFERRED_FIX |
| D2 | CUS-S18 | empty successful READ-list → generic `ดำเนินการเรียบร้อยค่ะ` | explicit "no shipments found for your account" (keep the generic reply for empty WRITE acks) | — | `decision_engine._summarize_action_result` empty-`full_mapped` branch | Med | PHASE_5_DEFERRED_FIX |
| D3 | CUS-S18 / E10 | no per-record TH-arrival DATE to build a real "today" filter | `DateArrivedTH` per record | FastTrade `SearchDataShipmentList` response | Med | EXTERNAL_API_DEPENDENCY |
| D4 | CUS-S08 / E7 | shipment/tracking → purchase-bill reverse map absent | `OrderCode` in `SearchDataShipment`/`SearchDataTracking` response, or a `SearchOrderByShipment` action | FastTrade | Med | EXTERNAL_API_DEPENDENCY |
| D5 | CUS-G12 | any status outside `_WAREHOUSE_ARRIVED_STATUS_RE` → "ยังไม่ถึงโกดังจีน" (over-commit for a genuinely undefined status) | "current status is X, cannot confirm receipt from that status" | — | `decision_engine._WAREHOUSE_ARRIVED_STATUS_RE` / warehouse branch | Low-Med | PHASE_5_DEFERRED_FIX |
| D6 | CUS-G16 / E3 | generic Order `Status`; no seller-dispatch field | `shop_shipped_at` | FastTrade `SearchDataOrder` | Med | EXTERNAL_API_DEPENDENCY |
| D7 | CUS-G16 | `ร้านส่งหรือยัง PO12345` (in-message PO) → `searchdataorder` read (safe) instead of a policy/status framing | keep it a status answer only | — | Business-Action keyword arbitration | Low | PHASE_5_DEFERRED_FIX |
| D8 | CUS-G17 | `ติดตามสถานะ สินค้า` → ambiguity-clarification prompt | source stage-1 identifier ask | — | action-selection multi-candidate tie | Low | PHASE_5_DEFERRED_FIX |
| D9 | CUS-S07 / F08 / G05 | invoice-issuance multi-turn eligibility loop not closed | ask product → (customer type / import mode) → confirm can issue | — | INVOICE family flow / `answer_planner.invoice_policy` | Med | PHASE_5_DEFERRED_FIX |
| D10 | CUS-S07 | `ขอใบกำกับของ PO12345` (in-message PO) → `searchdataorder` read instead of the how-to | stay on the invoice how-to | — | Business-Action keyword arbitration | Low | PHASE_5_DEFERRED_FIX |
| D11 | CUS-S07 / F10 | live KB retrieval historically missed ("ไม่มีข้อมูล") before the `บัญชี` chunk was seeded | reliable retrieval of the `บัญชี` invoice-download chunk | — | RAG retrieval confidence | Low | PHASE_6_REAL_LINE_UAT |
| D12 | CUS-G21 | no executable `CancelOrder`; `fixture_cancel_order` disabled | trusted, auth-scoped, confirmation-gated cancel + `payment_status` eligibility semantics | FastTrade | Med | EXTERNAL_API_DEPENDENCY |
| D13 | CUS-G21 | `ขอยกเลิก PO12345` (in-message PO) → `searchdataorder` read | stay on the cancel policy | — | Business-Action keyword arbitration | Low | PHASE_5_DEFERRED_FIX |
| D14 | CUS-G21 / R5 | cancel-policy KB chunk keyed on "สั่งซื้อสำเร็จ"; source frames it "ชำระ/ยังไม่ชำระ" | wording aligned to source (+ optional refund-timing policy) | — | KB `intent=สต็อก` chunk | Low | PHASE_3_RAG_COVERAGE |
| D15 | CUS-F05 | "สั่งแบตเตอรี่จำนวนเยอะได้ไหม" → rate answer (recorded FAIL) | prohibited-goods answer regardless of the "large quantity" framing | — | RAG family routing / `_OBJ_*` disambiguation | Med | PHASE_5_DEFERRED_FIX |
| D16 | CUS-G08 (+F01/F04) | can't enumerate prohibited sub-categories; "ครีมอาบน้ำ/น้ำหอม = ของเหลว" reasoning fragile | classify a named product into a prohibited category deterministically | — | prohibited-goods KB + `evidence_classifier` | Med | PHASE_3_RAG_COVERAGE + PHASE_5 |
| D17 | CUS-SC2 | self-service identity verification LOOPS (phone→email→handoff→re-loops) | on mismatch → single clean handoff; do not re-prompt on the next private question | desired flow under-specified | `services/customer_binding_service.py` / verification flow | **High** | PHASE_5_DEFERRED_FIX + CUSTOMER_CLARIFICATION |
| D18 | CUS-S20 | duplicate `case_id` | `CUS-S20a` / `CUS-S20b` | — | `customer_uat_master.jsonl` hygiene | Low | PHASE_5_DEFERRED_FIX |
| D19 | master hygiene | `expected_answer_constraints` empty on all 69 rows — reviewer "must / must not" rules have no machine-readable home | populate per-case constraints from PDF annotations | — | `customer_uat_master.jsonl` | Low | PHASE_5_DEFERRED_FIX |
| D20 | test coverage | Phase-1 per-workflow focused suites are scratchpad-only, not committed | promote to `tests/` | — | test suite | Med | PHASE_5_DEFERRED_FIX |

> D17 (SC2 verification loop) is flagged **High** because it is an
> auth/verification UX failure that dead-ends real customers. It is **not** a
> security *hole* (no data was disclosed to an unverified user — the failure is
> over-restriction + a loop), so it does not meet the "STOP immediately" bar,
> but it is the single highest-priority deferred item.

---

## 13. Final REAL LINE UAT Requirements

Every master row carries `requires_real_line: true` → **69 / 69** are
`FINAL_REAL_LINE_UAT_REQUIRED`. No historical result is treated as a Product
Owner REAL LINE PASS (Claude's offline / probe results are not acceptance).

Drivers, by cause:

| Driver | Cases |
|---|---|
| Live RAG retrieval confidence | all 33 RAG cases; esp. G09/G10/G14/G26 (family-guess history), F10/S07 (chunk added late), G22/G24 (wording fidelity) |
| LINE customer binding / verification | SC2, and every PRIVATE ERP case (G03,G11,G12,G16,G17,G18,G21,S01,S07-S09,S11-S13,S15,S17,S18) |
| Real conversation history / state-replay | RL-* fixtures, CSW9 multi-turn, calculator multi-turn, charter multi-turn |
| Production ERP data | all ERP-read cases |
| Media / image behaviour | the ~13 image-pending FAQ rows; CSW12 SP form image |
| Webhook / inbound attachment | G18 slip image, CSW11/CSW13 evidence |
| Funded LLM semantic routing | offline this phase the resolver is credit-exhausted → all family-dependent routing is REAL_LINE-only for final confirmation |

---

## 14. Future Phase Assignment (every gap)

| Next phase | Items |
|---|---|
| **PHASE_3_RAG_COVERAGE** | M2 (S12 SP image), M3/R4 (G09 no-claims rule), M4/R1 (13 image-pending FAQ images), M5/R2 (F03 transient clause), R5/D14 (G21 wording align), R6 (`546c1bd5` dedup), D16 partial (prohibited sub-categories), G20/G25/G26/G27/G28/F11 image attach |
| **PHASE_4_ADMIN_PROFILE_EXPORT** | (none from the customer source set — no admin/profile/export requirement in Ai.xlsx / ปัญหา / PDF / screenshots) |
| **PHASE_5_DEFERRED_FIX** | D1, D2, D5, D7, D8, D9, D10, D13, D15, D17, D18, D19, D20; CUS-G05/F08 loop; CUS-G17 routing; CUS-SC2 verification loop; master `expected_answer_constraints` population; S20 renumber; commit Phase-1 focused tests |
| **PHASE_6_REAL_LINE_UAT** | all 69 (`requires_real_line`), specifically the retrieval-confidence rows (G10, G14, G22, G24, F01–F04, F10, D11) and every PRIVATE ERP case with live binding |
| **EXTERNAL_API_DEPENDENCY** | E1 (ETA), E2 (items + claims), E3 (shop_shipped_at), E4 (wallet txns), E5 (cancel + payment_status), E6 (order-modify WRITE ×4: S02/S03/S04/S11), E7 (reverse map / S08), E8 (dup-list + delete / S13), E9 (address validate / S15), E10 (DateArrivedTH / S18), E11 (cheapest-carrier compare / M1) |
| **CUSTOMER_CLARIFICATION** | C1 (S07 route: PUBLIC RAG vs PRIVATE ERP), C2 (S05/S07/S12 how-to route label), C3 (P06 handoff channel), + D17's desired verification flow |
| **NO_ACTION_REQUIRED** | G04, G06, G07, G13, G15, G19, G23, G29/P20, S05, S06, S09, S10, S14, S16, S20(b), F06, F07, F09, RL-* |

---

## 15. Phase-2 Exit Decision

| Exit criterion | Status |
|---|---|
| every source file inspected | ✅ 8/8 |
| every XLSX sheet inspected | ✅ 8/8 (Ai.xlsx ×7 incl. meta, ปัญหา ×1) |
| all 17 PDF pages inspected | ⚠️ **text-layer + image count for all 17**; ~40 embedded screenshots NOT pixel-rendered (poppler unavailable) — annotations decoded, every referenced case resolves to a captured sheet row (same caveat as `CUSTOMER_UAT_SOURCE_MANIFEST.md`) |
| all standalone screenshots inspected | ✅ 5/5 (3 case rendered + read, 2 artifact) |
| every logical source requirement mapped | ✅ §3, §4 |
| source vs master mismatches listed | ✅ §5 |
| missing master cases listed | ✅ §5 (M1–M6) |
| duplicate cases listed | ✅ §10 (S20 ×2 + variant/linked map) |
| conflicts listed | ✅ §10 (C1–C3) |
| RAG gaps listed | ✅ §7 (R1–R6) |
| ERP/API gaps listed | ✅ §8 (E1–E11) |
| workflow gaps listed | ✅ §8 (M1 only) |
| test gaps listed | ✅ §11 |
| Deferred Fix Backlog consolidated | ✅ §12 (D1–D20) |
| every gap assigned to future phase | ✅ §14 |
| audit document saved | ✅ this file |
| production code unchanged | ✅ |
| production data unchanged | ✅ |
| Full Regression NOT RUN | ✅ |

**PHASE 2 STATUS: COMPLETE** (with the explicit poppler / PDF-pixel-render
caveat above — recommend a follow-up visual pass of the ~40 in-PDF screenshots
in an environment with a PDF renderer before Phase 6 sign-off).

**Production SHA: `0937d71951f1cdf2a073d97357f562092b744016` — UNCHANGED.**

**NEXT PHASE: PHASE 3 — RAG COVERAGE COMPLETION.**
