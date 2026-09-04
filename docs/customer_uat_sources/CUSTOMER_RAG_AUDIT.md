# CUSTOMER-RAG-AUDIT — remaining customer source gaps

Audited the 69-case customer UAT master
(`tests/customer_uat/customer_uat_master.jsonl`) against production
`45d3f00` via `decide()` + live RAG in the container.

## Classification of all 69 cases

| Bucket | Count | Case ids |
|---|---|---|
| RAG / public knowledge | 33 | G01,G02,G04,G05,G06,G07,G08,G09,G10,G13,G14,G15,G20,G22,G23,G24,G25,G26,G27,G28,S14,F01–F11,SC1,P07 |
| ERP / private | 22 | G03,G11,G12,G16,G17,G18,G21,S01–S05,S07–S09,S11–S13,S15,S17,S18,SC2 |
| Workflow / action | 3 | S06, S10, P20/G29/S20 (link) |
| Link conversion | 1 | CUS-P20 (also G29 / S20) |
| Attachment (answer OK, image not yet attached) | ~13 | the "13 correct but image pending" tally in `1.ถามเบื้องต้น` |
| Human-CS operational | 4 | G19, S06, S10, S16 |
| No-information / handoff (correct as-is) | 1 | CUS-P06 |
| Not applicable / meta | — | `Api Overall`, `all`, `API Summary` sheets; SecretCode row |

Only the **RAG / public-knowledge** subset was worked. TC19 (G19/SC1),
Calculator (G04/P07/SC3), Invoice-issuance/download (G05/F08/F10),
Link-conversion, ERP and Attachment cases were left to their own tracks
per the scope lock.

## RAG / public cases — per-case audit

Verified at `45d3f00` (before this change) with live retrieval:

| CASE | KNOWLEDGE EXISTS | RETRIEVAL | FAMILY | ACTIONABLE | ROOT CLASS |
|---|---|---|---|---|---|
| CUS-G01 ขอที่อยู่โกดัง | YES | PASS (asks ไทย/จีน — CUSTOMER-RAG-1) | GENERAL | unknown | ALREADY_PASS |
| CUS-G02 ที่อยู่โกดังจีน | YES | PASS (primary) | GENERAL/PICKUP_LOCATION | warehouse_location | ALREADY_PASS |
| CUS-G06 CBM คือ | YES | PASS | UNKNOWN | unknown | ALREADY_PASS |
| CUS-G07 ขั้นต่ำ | YES | PASS | UNKNOWN | payment_policy | ALREADY_PASS |
| CUS-G08 ห้ามนำเข้า | YES | PASS | PRODUCT_POLICY | prohibited_goods | ALREADY_PASS |
| CUS-G09 ชำระบิลสั่งซื้อ / **ชำระค่าสินค้ายังไง** | YES (`506370f4`) | **FAIL** on the "ชำระค่าสินค้ายังไง" paraphrase | UNKNOWN | payment_instruction | **RETRIEVAL_FAILURE** |
| CUS-G10 บิลขนส่งชำระได้เลยไหม | YES (`5d168a64` timing FAQ) | **FAIL** — LLM labelled it INVOICE, got the invoice answer | INVOICE (src=llm) | invoice_policy | **ANSWER_COMPOSITION** |
| CUS-G13 ระยะเวลาร้านจีน→โกดังจีน | YES | PASS | GENERAL | unknown | ALREADY_PASS |
| CUS-G14 ชำระบัตรเครดิตได้ไหม | YES (`a2618c6d`) | **FAIL** — LLM labelled it INVOICE, got the invoice answer | INVOICE (src=llm) | invoice_policy | **ANSWER_COMPOSITION** |
| CUS-G15 ขอเบอร์ติดต่อ | YES | PASS | UNKNOWN | service_information | ALREADY_PASS |
| CUS-G20 ขนส่งเอกชนมีอะไรบ้าง | YES | PASS ("Nim, EMS, J&T, Flash") | GENERAL | unknown | ALREADY_PASS |
| CUS-G22 เรทเท่าไหร่ | YES | PASS (full rate table / concise 5.11) | GENERAL/UNKNOWN | unknown | ALREADY_PASS (wording fidelity → human judge) |
| CUS-G23 คูปองคืนได้ไหม | YES | PASS | COUPON_USAGE | coupon_policy | ALREADY_PASS |
| CUS-G24 มีบริการอะไรบ้าง | YES | PASS (verbose company overview) | GENERAL | unknown | ALREADY_PASS (wording fidelity → human judge) |
| CUS-G25 ฝากสั่ง vs ฝากนำเข้า | YES | PASS | IMPORT_INTEREST | unknown | ALREADY_PASS |
| CUS-G26 ชำระบิลขนส่ง / **ชำระค่านำเข้ายังไง** | YES (`06636112`) | **FAIL** on the "ชำระค่านำเข้ายังไง" paraphrase → Fix-2 Human handoff | GENERAL | payment_instruction | **RETRIEVAL_FAILURE** |
| CUS-G27 ฝากสั่งค่าใช้จ่าย | YES | PASS | IMPORT_INTEREST | unknown | ALREADY_PASS |
| CUS-G28 / CUS-F11 ตีลังไม้ | YES | PASS | PRODUCT_POLICY/UNKNOWN | — | ALREADY_PASS |
| CUS-S14 ใช้คูปองยังไง | YES | PASS | COUPON_USAGE | coupon_policy | ALREADY_PASS |
| CUS-F01 ครีมอาบน้ำ / CUS-F04 น้ำหอม / CUS-F05 แบตเตอรี่ | YES | PASS (prohibited-goods answers) | PRODUCT_POLICY/IMPORT_INTEREST | prohibited_goods | ALREADY_PASS |
| CUS-F02 ทางรถกับทางเรือกี่วัน | YES | PASS ("7–10 / 14–20 วัน") | UNKNOWN | unknown | ALREADY_PASS (customer note contradicts the question asked) |
| CUS-F03 เรทนำเข้าเท่าไหร่ | YES | PASS | GENERAL | unknown | ALREADY_PASS |
| CUS-F06 โกดังอ่อนนุชเปิดทุกวัน | YES | PASS ("จ–ศ 9:00–18:00, หยุด ส–อา") | GENERAL | unknown | ALREADY_PASS |
| CUS-F07 มีเครื่องบินไหม | YES | PASS | UNKNOWN | unknown | ALREADY_PASS |
| CUS-F09 ส่งถึงหน้าบ้านไหม | YES | PASS | UNKNOWN | unknown | ALREADY_PASS |

## Root causes and fixes

### 1. CUS-G09 / CUS-G26 — RETRIEVAL_FAILURE (spell-corrector corruption)

The knowledge and the customer's own alt-phrasings already exist in
`506370f4` ("วิธีการชำระบิลสั่งซื้อ") and `06636112` ("ชำระบิลขนส่งยังไง")
and are embedded. The **fuzzy spell-corrector** (`rag/spell_correction.py`)
mangled the correctly-spelled queries before retrieval:

- `ชำระค่าสินค้ายังไง` → the "ะค่าสินค้า" window was fuzzy-corrected to the
  registered tag term **"เคลมสินค้า"** → routed into the claims FAQ,
  bare no-info reply.
- `ชำระค่านำเข้ายังไง` → the "่านำเข้า" window was fuzzy-corrected to
  **"การนำเข้า"** → no confident chunk → Fix-2 `unsupported_company_information`
  **Human handoff** on a public FAQ.

**Fix:** two entries added to the existing
`_FUZZY_CORRECTION_PHRASE_GUARDS` list (`re.compile(r"ค่าสินค้า")`,
`re.compile(r"ค่านำเข้า")`) — the same, established mechanism the list
already uses for "การนำเข้า", "ทางเรือ", "การสั่ง", etc. A genuine typo
elsewhere in the query still corrects.

### 2. CUS-G10 / CUS-G14 — ANSWER_COMPOSITION (LLM family guess)

The INVOICE-REGRESSION-1 branch opened on
`interpretation.intent_family == "INVOICE"`. For "บิลขนส่งชำระได้เลยไหม"
(shipping-bill timing) and "ชำระบัตรเครดิตได้ไหม" (credit-card policy) the
deterministic compositional tier returns UNKNOWN, but the **gated LLM
resolver GUESSED INVOICE** (by association — credit-card ↔ invoice), so
both got `_INVOICE_ISSUANCE_ANSWER` instead of their own FAQ.

**Fix:** `_invoice_issuance_branch_applies` now trusts the central INVOICE
family only when `interpretation.source == "deterministic"` (i.e. an
actual invoice noun — ใบกำกับ / ใบเสร็จ / tax invoice — was present). An
LLM family guess no longer drives the deterministic trusted-answer
branch. INVOICE CASE A/B/C and the download flow are unchanged
(deterministic family / literal recogniser).

## Duplicate / corrupted knowledge

| chunk id | source | active | conflict | superseded by |
|---|---|---|---|---|
| `546c1bd5` | Quick_FAQ_Patch (`40519066`) row 6, "ใบกำกับค่าสินค้าออกได้ไหม" | YES | Answer ends "…ไม่ทราบว่าสินค้าของลูกค้าเป็นอะไรคะ", omits the bill/payment condition, taxpayer-info and credit-card limitation | `ff288877` ("ออกใบกำกับได้ไหม", active) + `99390831` ("Shipify ออก e-Tax Invoice ได้ไหม", active), which carry the complete conditions; production routes around `546c1bd5` deterministically via INVOICE-REGRESSION-1 (`_INVOICE_ISSUANCE_ANSWER`). |

Smallest safe correction already applied (deterministic routing in
`45d3f00`). `546c1bd5` is **not deleted** — it remains the FAQ-exact
fallback for callers with no central interpretation, and deletion is a
broad KB change that the evidence does not require.

No other duplicate/conflict found in the RAG/public subset (payment,
warehouse, prohibited-goods, transit, coupon, crate rows are each
singular and clean).

## Deferred (not worked, per scope lock)

- **ERP / private (22):** G03,G11,G12,G16–G18,G21,S01–S05,S07–S09,S11–S13,S15,S17,S18,SC2
- **Action / workflow (2):** S06 (สั่งผลิตตามสเปค), S10 (รีแพ็ค)
- **Link conversion (1):** P20 / G29 / S20
- **Attachment (~13):** the "answer correct, image not yet attached" tally

## Status

**CODE PASS / DEPLOYED / READY FOR FINAL MANUAL UAT.** No REAL LINE
testing performed. Customer acceptance not declared.
