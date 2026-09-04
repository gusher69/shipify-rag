# SEMANTIC-FIRST-2.1 — close the remaining semantic-routing residuals

Production baseline: `737f4c6`. Three residuals from SEMANTIC-FIRST-2's
own verification, all "the central family was correct, downstream routing
did not honour it":

| # | Residual | Root cause | Fix |
|---|---|---|---|
| 1 | WAREHOUSE / PICKUP paraphrase → `getdatacustomer` / `searchdatashipmentlist` over-reach → `"ดำเนินการเรียบร้อยค่ะ"` / `"รายการบิลขนส่งทั้งหมด: 1 รายการ"` | `classify_turn_intent()` (lexical) returns `AMBIGUOUS` for a natural pickup-location paraphrase with no recognised question marker, so `exclude_private` is never set and identity-gated API actions stay eligible and win on generic parameter / mapped-field-keyword scoring. The central `PICKUP_LOCATION` family had no authority over `exclude_private`. | Decision Engine coercion (below). |
| 2 | COUPON-USAGE paraphrase → enters private / customer-id collection (`"รบกวนแจ้งรหัสลูกค้า…"`) | (a) same `AMBIGUOUS` → identity-gated eligible; (b) the RAG orchestrator's own `slot_filling_engine` matched `payment` on a bare `"…ตอนจ่ายเงิน"` checkout-step mention (no `_is_genuine_*` gate, unlike `warranty` / `invoice`); (c) `"ส่วนลด"` / `"discount"` were not synonyms of `"คูปอง"` so retrieval missed the coupon FAQ → General-Chat drift. | Coercion + `payment` gate + `coupon` synonym group. |
| 3 | INVOICE unseen (withholding-tax / document) paraphrase → weak General-Chat drift (`"…สำหรับสินค้าที่ผิดค่ะ"`) instead of an honest company no-info | An INVOICE-family question that names no `_COMPANY_OPERATIONAL_TOPIC_RE` term fell into the General Chat Fallback branch and got answered from the model's own general knowledge. | Orchestrator gate (below). |

## Fixes (all consume the ONE central interpretation — no new engine, no per-workflow LLM, no sentence lists)

### `services/conversation_semantics.py`
`PUBLIC_INFO_FAMILIES = {PICKUP_LOCATION, SELF_PICKUP, COUPON_USAGE, PRODUCT_POLICY, CHARTER_TRUCK, INVOICE}` —
the families that are ALWAYS a PUBLIC company-information question
(policy / how-to / location / service), never an identity-gated ERP or
customer-data lookup and never general chit-chat. Deliberately excludes
`SHIPMENT_STATUS` / `MY_COUPONS` (private-account), `ADDRESS_CHANGE`
(operational workflow), `SHIPPING_ESTIMATE` (own calculator flow).

### `services/decision_engine.py::decide()` — right after `classify_turn_intent`
When `semantic.intent_family ∈ PUBLIC_INFO_FAMILIES`, coerce
`turn_intent = "SHIPIFY_INFORMATION"` (the existing `exclude_private`
mechanism then drops every API / WEBHOOK action from `classify_question`,
the entity-continuation check and the fresh candidate search). Guards,
mirroring the established `public_clarification_continuity` coercion:

- never when the turn already reads `PRIVATE_ACTION` or a confirmed
  private-state inquiry owns it;
- never when the message carries a real account / order / record
  identifier (`ขอชื่อคูปองของลูกค้า SP1014` stays a private lookup);
- a **decisive** public-info reading (`interpret().confidence >= 0.75` —
  a full OBJECT+ACTION compositional shape) always wins; a **weaker**
  reading (`"คูปอง"` alone — could be `MY_COUPONS` or a how-to) only
  coerces when NO configured Business Action decisively claims the turn
  as its own (memory-free `search_candidate_actions` + `select_best_action`).

### `services/playground_orchestrator.py::run_playground_turn()` — General Chat Fallback gate (both branches)
Added `interpretation.intent_family ∈ PUBLIC_INFO_FAMILIES` to the
conditions that keep a turn OUT of the General-Chat branch. A company
family with no trusted evidence now reaches the Answerability Gate and
returns the honest company "no information" answer (and, via the existing
Fix-2 path, a Human-CS follow-up) — never a general-knowledge guess. No
business fact is invented; a genuinely unsupported document fact
(withholding tax) stays no-info.

### `services/slot_filling_engine.py::detect_erp_intent()`
New `_is_genuine_payment_lookup_request()` gate for the `payment` intent
— exactly mirroring the existing `warranty` / `invoice` gates. A bare
`"จ่ายเงิน"` / `"ชำระเงิน"` checkout-step mention inside an unrelated
how-to no longer triggers the customer-code collection; a real
outstanding-balance / self-referenced / numbered payment-status lookup
still does.

### `data/synonym_groups.json`
- new `coupon` group: `คูปอง` ↔ `ส่วนลด`, `โค้ดส่วนลด`, `โค้ดลดราคา`,
  `discount`, `discount code`, `voucher`, `โปรโมชั่นโค้ด`.
- `warehouse` group extended with `จุดรับพัสดุ`, `จุดรับของ`,
  `จุดโหลดสินค้า`, `จุดโหลดของ`, `สถานที่รับสินค้า`, `ที่รับสินค้า`.

Single domain terms (the sanctioned data-driven mechanism), never a
pasted failing sentence.

## Container verification (REAL LLM; no REAL LINE)

| Case | Result |
|---|---|
| WAREHOUSE `โกดังรับสินค้าอยู่ที่ไหน` / `ไปรับของแถวไหนครับ` / `จุดรับสินค้าของทางร้านอยู่ตรงไหน` / `ต้องไปรับของที่สาขาไหน` | `SHIPIFY_INFORMATION` → warehouse RAG (`"ต้องการที่อยู่โกดังไทยหรือโกดังจีนคะ"`). No ERP, no fake success. |
| WAREHOUSE unseen `จุดรับพัสดุฝั่งไทยอยู่โซนไหนของกรุงเทพครับ` | `SHIPIFY_INFORMATION` / `warehouse_location`; honest no-info on the oblique "which Bangkok zone" phrasing (**was** `searchdatashipmentlist` → `"รายการบิลขนส่งทั้งหมด: 1 รายการ"`). No ERP, no fake success. |
| SELF-PICKUP `อยากไปรับของเองต้องไปที่คลังไหนของบริษัท` | `SELF_PICKUP` → self-pickup-permission answer. |
| COUPON-USAGE `คูปองใช้ยังไง` / `ใช้ส่วนลดยังไงครับ` / `ต้องกดคูปองตรงไหน` / `ส่วนลดเอาไปใช้ยังไง` | `SHIPIFY_INFORMATION` / `coupon_policy` → full coupon steps. No identity collection. |
| COUPON-USAGE unseen `เอาโค้ดคูปองไปกรอกช่องไหนตอนจ่ายเงิน` | `SHIPIFY_INFORMATION` / `coupon_policy`; honest no-info on the oblique phrasing (**was** `"รบกวนแจ้งรหัสลูกค้า หรือเลขออเดอร์ค่ะ"`). No identity collection. |
| MY_COUPONS `ผมมีคูปองอะไรบ้าง` / `บัญชีผมเหลือคูปองไหม` / `คูปองในบัญชีผมมีอันไหนใช้ได้บ้าง` | `PRIVATE_ACTION` → verified: `getdatacustomer` lookup; unverified: `"กรุณาแจ้งรหัสลูกค้าค่ะ"` (authorization enforced). |
| INVOICE `ออกใบกำกับภาษีให้ได้ไหมคะ` / `โหลดใบกำกับภาษียังไง` | trusted issuance answer / download workflow — unchanged. |
| INVOICE unseen `อยากได้เอกสารหัก ณ ที่จ่ายของบิลนี้ทำไงคะ` / `ขอเอกสารรับรองการหักภาษี ณ ที่จ่ายจากทางบริษัทได้ไหม` | `INVOICE` family recognised; consistent honest company no-info via Human CS (**was** one General-Chat drift, one Fix-2). No fact invented. |
| Regression: shipment status `ร้านส่งหรือยังคะ` | asks for order id. |
| Regression: calculator `คิดค่าส่งจากจีนมาไทยยังไง` | opens the calculator. |
| Regression: product policy `นำเข้าครีมกันแดดได้ไหม` | prohibited verdict (cosmetics). |
| Regression: charter `เหมารถบรรทุกส่งของในไทยราคาเท่าไหร่` | honest no-info (TC19 slot-collection tests unchanged). |
| Regression: ERP read `เช็คสถานะพัสดุ FT318220260726001` | private lookup, VALID_NOT_FOUND. |
| Regression: Fix-2 true no-info `Shipify รับประกันว่าสินค้าทุกชิ้นจะผ่านศุลกากรไหม` | `HUMAN_HANDOFF` / `unsupported_company_information` — unchanged. |
| Regression: `ยอดเงินในบัญชีผมเหลือเท่าไหร่` | private wallet lookup. |

Known limit: a few maximally-oblique paraphrases (`ผมจะเอาคูปองไปใส่ตอนไหนดี`,
`จุดรับพัสดุ…โซนไหนของกรุงเทพ`) return an honest company no-info rather than
retrieving — the ROUTE is correct (PUBLIC, no ERP, no fake success, no
invention); the RAG recall gap on adversarially-oblique wording is out of
this task's scope ("do not rewrite warehouse RAG if knowledge exists").

## Regression

`python -m unittest` over `test_decision_engine`, `test_customer_uat_fix1/fix2`,
`test_conversation_semantics`, `test_customer_calc1/11`, `test_semantic_first_1/2/2_1`,
`test_sem1_private_state_routing`, `test_ppc1_referentless_clarification`,
`test_customer_rag1_pickup_location`, `test_customer_erp_read1`,
`test_customer_action1`, `test_invoice_product_regression2`,
`test_customer_rag_audit`, `test_slot_filling_engine`, `test_task06_authorization`,
`test_customer_rag2_1_charter_slots`, `test_synonym_*`,
`test_hybrid_question_classifier`, `test_fix23_product_interest_not_no_info`
— 783 tests, 6 failures, all pre-existing (3 `_CONFIG_CACHE` cross-test
leak in `test_decision_engine`; 3 explicit-new-Business-Action boundary
in `test_customer_uat_fix1`, unchanged since INVOICE-PRODUCT-REGRESSION-2).
New `tests/test_semantic_first_2_1.py` (21 tests).

## Status

**CODE PASS / DEPLOYED / READY FOR FINAL MANUAL UAT.** No REAL LINE
testing performed — the product owner performs final acceptance.
